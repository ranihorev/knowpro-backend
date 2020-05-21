from flask import Blueprint, request
import logging
from flask_jwt_extended import jwt_required, get_jwt_identity
from flask_restful import Api, Resource, abort, marshal_with
from .paper_query_utils import get_papers, papers_list_fields, get_paper_by_id
from .user_utils import add_to_library

app = Blueprint('library', __name__)
api = Api(app)
logger = logging.getLogger(__name__)


class Library(Resource):
    # TODO move to user routes
    method_decorators = [jwt_required]

    @marshal_with(papers_list_fields)
    def get(self):
        papers = get_papers(library=True)
        return papers


class SaveRemove(Resource):
    method_decorators = [jwt_required]

    def post(self, paper_id):
        current_user = get_jwt_identity()
        paper = get_paper_by_id(paper_id, {"_id": 1, "total_bookmarks": 1})
        op = request.url.split('/')[-1]
        if not paper:
            abort(404, message='Paper not found')
        add_to_library(op, current_user, paper)
        return {'message': 'success'}


api.add_resource(Library, "")
api.add_resource(SaveRemove, "/<paper_id>/save", "/<paper_id>/remove")