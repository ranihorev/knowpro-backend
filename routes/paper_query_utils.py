import datetime
import logging
import os
from typing import List

from flask_jwt_extended import get_jwt_identity

from .group_utils import get_group
from .s3_utils import arxiv_to_s3
from tasks.fetch_papers import fetch_entry
from .user_utils import get_user_library, find_by_email
from . import db_comments
from flask_restful import reqparse, fields, abort
from . import db_papers
from . import db_groups
import pymongo

logger = logging.getLogger(__name__)

SCORE_META = {'$meta': 'textScore'}
SORT_DICT = {'tweets': 'twtr_sum', 'date': 'time_published', 'score': 'score', 'bookmarks': 'total_bookmarks'}
AGE_DICT = {'day': 1, '3days': 3, 'week': 7, 'month': 30, 'year': 365, 'all': -1}

query_parser = reqparse.RequestParser()
query_parser.add_argument('q', type=str, required=False)
query_parser.add_argument('author', type=str, required=False)
query_parser.add_argument('page_num', type=int, required=False, default=1)
query_parser.add_argument('sort', type=str, required=False, choices=list(SORT_DICT.keys()))
query_parser.add_argument('age', type=str, required=False, choices=list(AGE_DICT.keys()), default='week')
query_parser.add_argument('categories', type=str, required=False)
query_parser.add_argument('group', type=str, required=False)


class TwitterUrl(fields.Raw):
    def format(self, objs):
        links = []
        for obj in objs:
            link = 'https://twitter.com/' + obj['tname'] + '/status/' + obj['tid']
            score = obj['likes'] + 2 * obj['rt'] + 4 * obj.get('replies', 0)
            links.append({'link': link, 'name': obj['tname'], 'score': score})
        return links


class Github(fields.Raw):
    def format(self, obj):
        if not obj.get('github_link'):
            return None
        return {
            'github': obj['github_link'],
            'stars': obj.get('stars', 0),
            'paperswithcode': obj.get('paperswithcode_link')
        }


papers_fields = {
    '_id': fields.String,
    'title': fields.String,
    'saved_in_library': fields.Boolean,
    'authors': fields.Nested({'name': fields.String}),
    'time_published': fields.DateTime(dt_format='rfc822'),
    'summary': fields.String,
    'twtr_score': fields.Integer(attribute='twtr_sum'),
    'twtr_links': TwitterUrl(attribute='twtr_links'),
    'bookmarks_count': fields.Integer(attribute='total_bookmarks'),
    'comments_count': fields.Integer,
    'github': Github(attribute='code'),
    'groups': fields.Raw
}

papers_list_fields = {
    'papers': fields.Nested(papers_fields),
    'count': fields.Integer,
}


def sort_papers(papers, args):
    field = args.get('sort', 'date')
    order = pymongo.DESCENDING
    if field == 'score':
        order = SCORE_META
    return papers.sort(SORT_DICT[field], order)


def get_papers(library=False, page_size=20):
    current_user = get_jwt_identity()

    # Get arguments
    args = query_parser.parse_args()
    q = args['q']
    author = args['author']
    page_num = args['page_num']
    age = args['age']
    categories = args['categories']
    group_id = args['group']

    # Calculates skip for pagination
    skips = page_size * (page_num - 1)

    filters = {}
    if author:
        filters['authors.name'] = author

    if library:
        user_library = get_user_library(current_user)
        filters["_id"] = {"$in": user_library}

    if age != 'all':
        dnow_utc = datetime.datetime.now()
        dminus = dnow_utc - datetime.timedelta(days=int(AGE_DICT[age]))
        filters['time_published'] = {'$gt': dminus}

    if categories:
        filters['tags.term'] = {"$in": categories.split(';')}

    if group_id:
        group, _ = get_group(group_id)
        group_papers = group.get('papers', [])
        filters['_id'] = {'$in': group_papers}

    facet = {
        'papers': [
            {'$skip': skips}, {'$limit': page_size},
        ],
    }
    if page_num == 1:
        facet['count'] = [
            {"$count": "count"}
        ]

    agg_query = [
        {'$match': filters},
        {'$lookup': {
            'from': 'group_papers',
            'localField': '_id',
            'foreignField': 'paper_id',
            'as': 'group'}
        },
        {'$facet': facet }
    ]
    if q:
        filters['$text'] = {'$search': q}
        results = db_papers.find(filters, {'score': SCORE_META})
    else:
        results = db_papers.aggregate(agg_query)

    # papers = sort_papers(papers, args)
    results = list(results)[0]

    # Adds stats to query
    papers = include_stats(results.get('papers'), user=current_user)
    count = -1
    if 'count' in results:
        count = results['count'][0]['count']

    return {'papers': papers, 'count': count}


def get_comments_count():
    papers_comments = {}
    papers_comments_list = list(db_comments.aggregate([
        {
            "$match": {
                "visibility.type": {"$in": ["public", "anonymous"]}
            }
        },
        {
            "$group":
                {
                    "_id": "$pid",
                    "comments_count": {
                        "$sum": 1
                    }
                }
        }]))
    for comments in papers_comments_list:
        papers_comments[comments['_id']] = comments['comments_count']

    return papers_comments


def get_paper_groups(user_email: str, paper_ids: List[str]):
    user = find_by_email(user_email, fields={'_id': 1, 'groups': 1})
    groups = list(db_groups.find(
        {'$and': [{'_id': {'$in': user.get('groups', [])}}, {'papers': {'$in': paper_ids}}, {'users': user['_id']}]}))
    return groups


def include_stats(papers, library=None, user=None):
    # Get comments count for each paper
    papers_comments = get_comments_count()

    # Get the current user's library to toggle available papers
    if not library:
        library = get_user_library(user)

    groups = []
    if user:
        groups = get_paper_groups(user, [paper['_id'] for paper in papers])

    # For each paper we store the comments, library toggle and thumbs
    for paper in papers:
        paper_id = paper['_id']
        paper['comments_count'] = papers_comments.get(paper_id, 0)
        paper['saved_in_library'] = paper_id in library

    return papers


def abs_to_pdf(url):
    return url.replace('abs', 'pdf').replace('http', 'https') + '.pdf'


def get_paper_with_pdf(paper_id):
    paper = db_papers.find_one(paper_id)
    if not paper:
        # Fetch from arxiv
        paper = fetch_entry(paper_id)
        if not paper:
            abort(404, message='Paper not found')

        paper['_id'] = paper['id']
    pdf_url = abs_to_pdf(paper['link'])

    if os.environ.get('S3_BUCKET_NAME'):
        pdf_url = arxiv_to_s3(pdf_url)

    paper['pdf_link'] = pdf_url
    return paper
