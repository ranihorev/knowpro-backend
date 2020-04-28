import logging
import uuid
from datetime import datetime
from enum import Enum

from bson import ObjectId
from flask import Blueprint
from flask_jwt_extended import get_jwt_identity, jwt_optional, jwt_required
from flask_restful import Api, Resource, abort, fields, marshal_with, reqparse

from sqlalchemy import or_
from src.new_backend.models import Collection, Comment, Paper, db, Author

from .acronym_extractor import extract_acronyms
from .latex_utils import REFERENCES_VERSION, extract_references_from_latex
from .paper_query_utils import (PUBLIC_TYPES, Github, get_paper_with_pdf, include_stats)
from .query_utils import fix_paper_id
from .user_utils import add_user_data, find_by_email, get_user
from src.routes.s3_utils import key_to_url

app = Blueprint('paper', __name__)
api = Api(app)
logger = logging.getLogger(__name__)

tot_votes = 0
last_reddit_update = None


class ItemState(Enum):
    existing = 1
    updated = 2
    new = 3


def visibilityObj(obj):
    choices = ('public', 'private', 'anonymous', 'group')
    vis_type = obj.get('type')
    if vis_type not in choices:
        raise ValueError('Visibility value is incorrect')
    if vis_type == 'group' and not obj.get('id'):
        raise ValueError('Group id is missing')
    return obj


EMPTY_FIELD_MSG = 'This field cannot be blank'

new_reply_parser = reqparse.RequestParser()
new_reply_parser.add_argument('text', help='This field cannot be blank', type=str, location='json', required=True)

paper_fields = {
    'url': fields.String(attribute='local_pdf'),
    'title': fields.String,
    'authors': fields.Nested({'name': fields.String}),
    'time_published': fields.DateTime(attribute='publication_date', dt_format='rfc822'),
    'abstract': fields.String,
    'code': Github(attribute='code'),
    'groups': fields.Nested({'id': fields.Integer, 'name': fields.String}),
    'is_editable': fields.Boolean(attribute='is_private', default=False)
}


class PaperResource(Resource):
    method_decorators = [jwt_optional]

    @marshal_with(paper_fields)
    def get(self, paper_id):
        current_user = get_jwt_identity()
        paper = get_paper_with_pdf(paper_id)
        if current_user:
            user = get_user()
            paper.groups = Collection.query.filter(Collection.users.any(
                id=user.id), Collection.papers.any(id=paper.id)).all()
        return paper


replies_fields = {
    'id': fields.String,
    'user': fields.String(attribute='user.username'),
    'text': fields.String,
    'createdAt': fields.DateTime(dt_format='rfc822', attribute='created_at'),
}


comment_fields = {
    'id': fields.String(),
    'content': fields.Raw(),
    'comment': fields.Raw(attribute='highlighted_text'),
    'position': fields.Raw,
    'user': fields.String(attribute='user.username'),
    # 'canEdit': fields.Boolean(),
    'createdAt': fields.DateTime(dt_format='rfc822', attribute='creation_date'),
    # 'replies': fields.List(fields.Nested(replies_fields)),
    'visibility': fields.String(attribute='shared_with'),
    'isGeneral': fields.Boolean(attribute='is_general'),
}


def get_visibility(comment):
    if isinstance(comment['visibility'], dict):
        return comment['visibility'].get('type', '')
    return comment['visibility']


def add_metadata(comments):
    current_user = get_jwt_identity()

    def add_single_meta(comment):
        comment['canEdit'] = (current_user and current_user == comment['user'].get('email', -1))
        if get_visibility(comment) == 'anonymous':
            comment['user']['username'] = 'Anonymous'

    if isinstance(comments, list):
        for c in comments:
            add_single_meta(c)
    else:
        add_single_meta(comments)


def anonymize_user(paper: Paper):
    if paper.shared_with == 'anonymous':
        return ''
    else:
        return paper.user.username


def can_edit(paper: Paper):
    current_user = get_jwt_identity()
    if not current_user:
        return False
    return paper.user.email == current_user


comment_fields = {
    'id': fields.String(),
    'content': fields.Raw(),
    'comment': fields.Raw(attribute='highlighted_text'),
    'position': fields.Raw,
    'username': fields.String(attribute=lambda x: anonymize_user(x)),
    'canEdit': fields.String(attribute=lambda x: can_edit(x)),
    'createdAt': fields.DateTime(dt_format='rfc822', attribute='creation_date'),
    # 'replies': fields.List(fields.Nested(replies_fields)),
    'visibility': fields.String(attribute='shared_with'),
    'isGeneral': fields.Boolean(attribute='is_general'),
}


class CommentsResource(Resource):
    method_decorators = [jwt_optional]

    @marshal_with(comment_fields, envelope='comments')
    def get(self, paper_id):
        parser = reqparse.RequestParser()
        parser.add_argument('group', required=False, location='args')
        group_id = parser.parse_args().get('group')
        user = get_user()
        query = Comment.query.filter(Comment.paper_id == paper_id)
        if group_id:
            query = query.filter(Comment.collection_id == group_id)
        else:
            if user:
                query = query.filter(Comment.shared_with.in_(PUBLIC_TYPES))
            else:
                query = query.filter(or_(Comment.shared_with.in_(PUBLIC_TYPES), Comment.user_id == user.id))
        return query.all()


class NewCommentResource(Resource):
    method_decorators = [jwt_optional]

    @marshal_with(comment_fields, envelope='comment')
    def post(self, paper_id):
        new_comment_parser = reqparse.RequestParser()
        new_comment_parser.add_argument('comment', help=EMPTY_FIELD_MSG, type=str, location='json')
        new_comment_parser.add_argument('content', help=EMPTY_FIELD_MSG, type=str, location='json')
        new_comment_parser.add_argument('position', type=dict, location='json')
        new_comment_parser.add_argument('isGeneral', type=bool, location='json')
        new_comment_parser.add_argument('visibility', help=EMPTY_FIELD_MSG, type=visibilityObj, location='json',
                                        required=True)
        data = new_comment_parser.parse_args()
        is_general = data['isGeneral'] is not None
        if not is_general and (data['position'] is None or data['content'] is None):
            abort(401, message='position or content are missing for non-general comment')

        if is_general:
            data['position'] = None
            data['content'] = None
        else:
            del data['isGeneral']

        visibility = data['visibility']
        if visibility['type'] != 'public' and not get_jwt_identity():
            abort(401, message='Please log in to submit non-public comments')

        collection_id = None
        if visibility.get('type') == 'group':
            collection_id = Collection.query.get_or_404(visibility.get('id'))

        paper = Paper.query.get_or_404(paper_id)

        try:
            user_id = get_user().id
        except Exception:
            user_id = None

        comment = Comment(highlighted_text=data['content'], text=data['comment'], paper_id=paper.id, is_general=is_general, shared_with=visibility['type'],
                          creation_date=datetime.utcnow(), user_id=user_id, position=data['position'], collection_id=collection_id)

        db.session.add(comment)
        db.session.commit()
        return comment


class CommentResource(Resource):
    method_decorators = [jwt_required]

    def _get_comment(self, comment_id):
        comment = Comment.query.get_or_404(comment_id)
        current_user = get_jwt_identity()

        if comment.user.email != current_user:
            abort(403, message='unauthorized to delete comment')

    @marshal_with(comment_fields, envelope='comment')
    def patch(self, comment_id):
        comment = self._get_comment(comment_id)
        edit_comment_parser = reqparse.RequestParser()
        edit_comment_parser.add_argument('comment', help=EMPTY_FIELD_MSG, type=str, location='json', required=False)
        edit_comment_parser.add_argument('visibility', help=EMPTY_FIELD_MSG, type=visibilityObj, location='json',
                                         required=True)
        data = edit_comment_parser.parse_args()
        comment.text = data['comment']
        comment.shared_with = data['visibility']['type']
        comment.shared_with = data['visibility']['id'] if data['visibility']['type'] == 'group' else None
        db.session.commit()

        add_metadata(comment)
        return comment

    def delete(self, comment_id):
        comment = self._get_comment(comment_id)
        db.session.delete(comment)
        db.session.commit()

        return {'message': 'success'}


class ReplyResource(Resource):
    method_decorators = [jwt_optional]

    def _get_comment(self, comment_id):
        comment = db.session.query(CommentResource).filter(CommentResource.id == comment_id).first()
        if not comment:
            abort(404, messsage='Comment not found')
        return comment

    @marshal_with(comment_fields, envelope='comment')
    def post(self, comment_id):
        comment = self._get_comment(comment_id)
        data = new_reply_parser.parse_args()
        data['created_at'] = datetime.utcnow()
        data['id'] = str(uuid.uuid4())
        add_user_data(data)
        new_values = {"$push": {'replies': data}}
        db_comments.update_one(comment_id, new_values)
        comment = self._get_comment(comment_id)
        add_metadata(comment)
        return comment


def get_paper_item(paper, item, latex_fn, version=None, force_update=False):
    state = ItemState.existing
    if not paper:
        abort(404, message='Paper not found')
    new_value = old_value = getattr(paper, item)

    if force_update or not old_value or (version is not None and float(old_value.get('version', 0)) < version):
        state = ItemState.new if not old_value else ItemState.updated

        try:
            new_value = latex_fn(paper.original_id)
            setattr(paper, item, new_value)
        except Exception as e:
            logger.error(f'Failed to retrieve {item} for {paper.id} - {e}')
            abort(500, message=f'Failed to retrieve {item}')
    return new_value, old_value, state


class PaperReferencesResource(Resource):
    method_decorators = [jwt_optional]

    def get(self, paper_id):
        query_parser = reqparse.RequestParser()
        query_parser.add_argument('force', type=str, required=False)
        paper = Paper.query.get_or_404(paper_id)

        # Rani: how to address private papers?
        if paper.is_private:
            return []

        force_update = bool(query_parser.parse_args().get('force'))
        references, _, _ = get_paper_item(paper, 'references', extract_references_from_latex, REFERENCES_VERSION,
                                          force_update=force_update)
        return references['data']


class PaperAcronymsResource(Resource):
    method_decorators = [jwt_optional]

    def _update_acronyms_counter(self, acronyms, inc_value=1):
        for short_form, long_form in acronyms.items():
            db_acronyms.update({'short_form': short_form}, {'$inc': {f'long_form.{long_form}': inc_value}}, True)

    def _enrich_matches(self, matches, short_forms):
        additional_matches = db_acronyms.find({"short_form": {"$in": short_forms}})
        for m in additional_matches:
            cur_short_form = m.get('short_form')
            if m.get('verified'):
                matches[cur_short_form] = m.get('verified')
            elif cur_short_form in matches:
                pass
            else:
                long_forms = m.get('long_form')
                if long_forms:
                    most_common = max(long_forms,
                                      key=(lambda key: long_forms[key] if isinstance(long_forms[key], int) else 0))
                    matches[cur_short_form] = most_common
        return matches

    def get(self, paper_id):
        new_acronyms, old_acronyms, state = get_paper_item(paper_id, 'acronyms', extract_acronyms)
        if state == ItemState.new:
            self._update_acronyms_counter(new_acronyms["matches"])
        elif state == ItemState.updated:
            self._update_acronyms_counter(old_acronyms["matches"], -1)
            self._update_acronyms_counter(new_acronyms["matches"], 1)
        matches = self._enrich_matches(new_acronyms['matches'], new_acronyms['short_forms'])
        return matches

    method_decorators = [jwt_required]

    @marshal_with(paper_fields)
    def post(self):
        current_user = get_jwt_identity()
        parser = reqparse.RequestParser()
        parser.add_argument('id', type=str, required=False)
        parser.add_argument('title', type=str, required=True)
        parser.add_argument('date', type=lambda x: datetime.strptime(x, '%Y-%m-%dT%H:%M:%S.%fZ'), required=True,
                            dest="publication_date")
        parser.add_argument('md5', type=str, required=False)
        parser.add_argument('abstract', type=str, required=True)
        parser.add_argument('authors', type=str, required=True, action="append")
        paper_data = parser.parse_args()

        if 'id' not in paper_data and 'md5' not in paper_data:
            abort(403)

        # If the paper didn't exist in our database (or it's a new version), we add it
        paper = db.session.query(Paper).filter(Paper.id == paper_data['id']).first()

        paper_data['pdf_link'] = key_to_url(paper_data['md5'], with_prefix=True) + '.pdf'
        paper_data['last_update_date'] = datetime.utcnow()
        paper_data['is_private'] = True

        if not paper:
            paper = Paper(title=paper_data['title'], pdf_link=paper_data['pdf_link'], publication_date=paper_data['publication_date'],
                          abstract=paper_data['abstract'], last_update_date=paper_data['last_update_date'], is_private=paper_data['is_private'])
            db.session.add(paper)
        else:
            paper.title = paper_data['title']
            paper.pdf_link = paper_data['pdf_link']
            paper.publication_date = paper_data['publication_date']
            paper.abstract = paper_data['abstract']

        for author_name in paper_data['authors']:
            existing_author = db.session.query(Author).filter(Author.name == author_name).first()

            if not existing_author:
                new_author = Author(name=author_name)
                new_author.papers.append(paper)
                db.session.add(new_author)

        db.session.commit()

        return {'paper_id': str(paper.id)}


# Still need to be converted from Mongo to PostGres
api.add_resource(ReplyResource, "/<paper_id>/comment/<comment_id>/reply")
api.add_resource(PaperAcronymsResource, "/<paper_id>/acronyms")

# Done (untested)
api.add_resource(CommentsResource, "/<paper_id>/comments")
api.add_resource(PaperResource, "/<paper_id>")
api.add_resource(NewCommentResource, "/<paper_id>/new_comment")
api.add_resource(CommentResource, "/<paper_id>/comment/<comment_id>")
api.add_resource(PaperReferencesResource, "/<paper_id>/references")
api.add_resource(EditPaperResource, "/edit")
