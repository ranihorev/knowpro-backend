import logging
from datetime import datetime
from typing import Optional

from flask import Blueprint
from flask_jwt_extended import get_jwt_identity, jwt_optional, jwt_required
from flask_restful import (Api, Resource, abort, fields, inputs, marshal,
                           marshal_with, reqparse)
from flask_socketio import emit
from sqlalchemy import or_

from ..models import Collection, Comment, Paper, Reply, db
from .notifications.index import new_comment_notification, new_reply_notification
from .paper_query_utils import PUBLIC_TYPES
from .permissions_utils import enforce_permissions_to_paper
from .user_utils import get_jwt_email, get_user_by_email, get_user_optional
from .utils import start_background_task

app = Blueprint('comments', __name__)
api = Api(app)
logger = logging.getLogger(__name__)

EMPTY_FIELD_MSG = 'This field cannot be blank'


def anonymize_user(comment: Comment, field: str):
    if comment.shared_with == 'anonymous' or not comment.user:
        return ''
    else:
        return getattr(comment.user, field)


def can_edit(comment: Comment):
    current_user = get_jwt_email()
    if not current_user:
        return False
    return comment.user and comment.user.email == current_user


visibility_fields = {
    'type': fields.String(attribute='shared_with'),
    'id': fields.String(attribute='collection_id')
}

replies_fields = {
    'id': fields.String,
    'user': fields.String(attribute='user.username'),
    'first_name': fields.String(attribute='user.first_name'),
    'last_name': fields.String(attribute='user.last_name'),
    'text': fields.String,
    'createdAt': fields.DateTime(dt_format='rfc822', attribute='creation_date'),
}

comment_fields = {
    'id': fields.String,
    'text': fields.String(attribute='text'),
    'highlighted_text': fields.String(attribute='highlighted_text'),
    'position': fields.Raw,
    'username': fields.String(attribute=lambda x: anonymize_user(x, 'username')),
    'first_name': fields.String(attribute=lambda x: anonymize_user(x, 'first_name')),
    'last_name': fields.String(attribute=lambda x: anonymize_user(x, 'last_name')),
    'canEdit': fields.Boolean(attribute=lambda x: can_edit(x)),
    'createdAt': fields.DateTime(dt_format='rfc822', attribute='creation_date'),
    'replies': fields.List(fields.Nested(replies_fields)),
    'visibility': visibility_fields,
    'isGeneral': fields.Boolean(attribute='is_general'),
}


def visibilityObj(obj):
    choices = ('public', 'private', 'anonymous', 'group')
    vis_type = obj.get('type')
    if vis_type not in choices:
        raise ValueError('Visibility value is incorrect')
    if vis_type == 'group' and not obj.get('id'):
        raise ValueError('Group id is missing')
    return obj


class CommentsResource(Resource):
    method_decorators = [jwt_optional]

    @marshal_with(comment_fields, envelope='comments')
    def get(self, paper_id):
        parser = reqparse.RequestParser()
        parser.add_argument('group', required=False, location='args')
        group_id = parser.parse_args().get('group')
        user = get_user_optional()
        paper = Paper.query.get_or_404(paper_id)
        enforce_permissions_to_paper(paper, user)
        query = Comment.query.filter(Comment.paper_id == paper_id)
        # TODO: simplify this:
        if group_id:
            query = query.filter(Comment.collection_id == group_id)
        else:
            if user:
                query = query.filter(or_(Comment.shared_with.in_(PUBLIC_TYPES), Comment.user_id == user.id))
            else:
                query = query.filter(Comment.shared_with.in_(PUBLIC_TYPES))
        return query.all()


def emit_update_to_paper_subscribers(paper_id: str, type: str, comment: Comment):
    try:
        emit('comment', {'type': type, 'data': marshal(comment, comment_fields)}, to=str(paper_id), namespace="/")
    except Exception as e:
        logger.error(e)


class NewCommentResource(Resource):
    method_decorators = [jwt_required]

    def notify_if_needed(self, user_id: Optional[int], paper: Paper, comment: Comment):
        try:
            start_background_task(target=new_comment_notification, user_id=user_id,
                                  paper_id=paper.id, comment_id=comment.id)
        except Exception as e:
            logger.error(f'Failed to notify on a new comment - {e}')

    @marshal_with(comment_fields, envelope='comment')
    def post(self, paper_id):
        new_comment_parser = reqparse.RequestParser()
        new_comment_parser.add_argument('text', help=EMPTY_FIELD_MSG, type=str, location='json')
        new_comment_parser.add_argument('highlighted_text', help=EMPTY_FIELD_MSG, type=str, location='json')
        new_comment_parser.add_argument('position', type=dict, location='json')
        new_comment_parser.add_argument('isGeneral', type=inputs.boolean, location='json')
        new_comment_parser.add_argument('visibility', help=EMPTY_FIELD_MSG, type=visibilityObj, location='json',
                                        required=True)
        data = new_comment_parser.parse_args()
        is_general = data['isGeneral'] is not None
        if not is_general and (data['position'] is None or data['highlighted_text'] is None):
            abort(401, message='position or content are missing for non-general comment')

        if is_general:
            data['position'] = None
            data['highlighted_text'] = None
        else:
            del data['isGeneral']

        visibility = data['visibility']
        if visibility['type'] != 'public' and not get_jwt_identity():
            abort(401, message='Please log in to submit non-public comments')

        collection_id = None
        if visibility.get('type') == 'group':
            collection_id = Collection.query.get_or_404(visibility.get('id')).id

        paper = Paper.query.get_or_404(paper_id)

        user = get_user_by_email()
        user_id = user.id
        enforce_permissions_to_paper(paper, user)

        comment = Comment(highlighted_text=data['highlighted_text'], text=data['text'], paper_id=paper.id, is_general=is_general, shared_with=visibility['type'],
                          creation_date=datetime.utcnow(), user_id=user_id, position=data['position'], collection_id=collection_id)

        db.session.add(comment)
        db.session.commit()
        self.notify_if_needed(user_id, paper, comment)
        emit_update_to_paper_subscribers(paper_id, 'new', comment)
        return comment


class CommentResource(Resource):
    method_decorators = [jwt_required]

    def _get_comment(self, comment_id) -> Comment:
        comment = Comment.query.get_or_404(comment_id)
        current_user = get_jwt_email()

        if comment.user.email != current_user:
            abort(403, message='unauthorized to delete comment')
        return comment

    @marshal_with(comment_fields, envelope='comment')
    def patch(self, comment_id):
        comment = self._get_comment(comment_id)
        edit_comment_parser = reqparse.RequestParser()
        edit_comment_parser.add_argument('text', help=EMPTY_FIELD_MSG, type=str, location='json', required=False)
        edit_comment_parser.add_argument('visibility', help=EMPTY_FIELD_MSG, type=visibilityObj, location='json',
                                         required=True)
        data = edit_comment_parser.parse_args()
        comment.text = data['text']
        comment.shared_with = data['visibility']['type']
        comment.collection_id = data['visibility']['id'] if data['visibility']['type'] == 'group' else None
        db.session.commit()
        emit_update_to_paper_subscribers(comment.paper_id, 'update', comment)
        return comment

    def delete(self, comment_id):
        comment = self._get_comment(comment_id)
        paper_id = str(comment.paper_id)
        db.session.delete(comment)
        db.session.commit()
        try:
            emit('comment', {'type': 'delete', 'id': comment_id}, to=paper_id, namespace='/')
        except Exception as e:
            logger.error(e)
        return {'message': 'success'}


class ReplyResource(Resource):
    method_decorators = [jwt_required]

    @marshal_with(comment_fields, envelope='comment')
    def post(self, comment_id):
        comment: Comment = Comment.query.get_or_404(comment_id)
        new_reply_parser = reqparse.RequestParser()
        new_reply_parser.add_argument('text', help='This field cannot be blank',
                                      type=str, location='json', required=True)
        data = new_reply_parser.parse_args()
        user = get_user_by_email()

        enforce_permissions_to_paper(comment.paper, user)

        reply = Reply(parent_id=comment.id, text=data['text'], user_id=user.id if user else None)
        db.session.add(reply)
        db.session.commit()
        db.session.refresh(comment)
        emit_update_to_paper_subscribers(comment.paper_id, 'update', comment)
        try:
            start_background_task(target=new_reply_notification, user_id=user.id,
                                  paper_id=comment.paper_id, reply_id=reply.id)
        except Exception as e:
            logger.error(f'Failed to notify on a new comment - {e}')
        return comment


api.add_resource(ReplyResource, "/comment/<comment_id>/reply")
api.add_resource(CommentResource, "/comment/<comment_id>")
api.add_resource(CommentsResource, "/<paper_id>/comments")
api.add_resource(NewCommentResource, "/<paper_id>/new_comment")
