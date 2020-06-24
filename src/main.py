import logging
import os
import click

from src import app
# create the DB:
from .new_backend.models import db, Paper, paper_collection_table
from sqlalchemy import func
from flask_cors import CORS
from flask_jwt_extended import JWTManager
from flask_jwt_extended.exceptions import NoAuthorizationError
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from flask_caching import Cache
from .routes.paper import app as paper_routes
from .routes.comments import app as comments_routes
from .routes.replies import app as replies_routes
from .routes.paper_list import app as paper_list_routes
from .routes.user import app as user_routes
from .routes.groups import app as groups_routes
from .routes.admin import app as admin_routes
from .routes.new_paper import app as new_paper_routes
from .new_backend.scrapers import arxiv
from .new_backend.scrapers import paperswithcode
import sentry_sdk
from sentry_sdk.integrations.flask import FlaskIntegration
from src.new_backend.scrapers import twitter
import threading
from .run_background_tasks import run_scheduled_tasks
from .mongo_to_postgres import migrate


env = os.environ.get('ENV', 'development')

logger = logging.getLogger(__name__)

SENTRY_DSN = os.environ.get('SENTRY_DSN', '')


def before_send(event, hint):
    if 'exc_info' in hint:
        exc_type, exc_value, tb = hint['exc_info']
        if isinstance(exc_value, NoAuthorizationError):
            req = event.get('request', '')
            logger.warning(f'Unauthorized access - {req}')
            return None
    return event


if SENTRY_DSN:
    sentry_sdk.init(
        dsn=SENTRY_DSN,
        integrations=[FlaskIntegration()],
        environment=env,
        before_send=before_send,
        ignore_errors=['TooManyRequests']
    )

app.config['ENV'] = env

# TODO: fix this:
cors = CORS(app, supports_credentials=True, origins=['*'])

if os.path.isfile('secret_key.txt'):
    app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY')
else:
    app.config['SECRET_KEY'] = 'devkey, should be in a file'

app.config['JWT_TOKEN_LOCATION'] = ['cookies']
app.config['JWT_COOKIE_CSRF_PROTECT'] = False
app.config['JWT_ACCESS_TOKEN_EXPIRES'] = False

jwt = JWTManager(app)

limiter = Limiter(app, key_func=get_remote_address, default_limits=[
    "5000 per hour", "200 per minute"])
cache = Cache(app, config={'CACHE_TYPE': 'simple'})

app.register_blueprint(paper_list_routes, url_prefix='/papers')
app.register_blueprint(paper_routes, url_prefix='/paper')
app.register_blueprint(replies_routes, url_prefix='/reply')
app.register_blueprint(comments_routes, url_prefix='/paper')
app.register_blueprint(user_routes, url_prefix='/user')
app.register_blueprint(groups_routes, url_prefix='/groups')
app.register_blueprint(admin_routes, url_prefix='/admin')
app.register_blueprint(new_paper_routes, url_prefix='/new_paper')


@app.cli.command("fetch-arxiv")
def fetch_arxiv():
    arxiv.run()


@app.cli.command("fetch-paperswithcode")
def fetch_papers_with_code():
    paperswithcode.run()


@app.cli.command("fetch-twitter")
def fetch_twitter():
    twitter.main_twitter_fetcher()


@app.cli.command("run-background-tasks")
def background_tasks():
    run_scheduled_tasks()


@app.route('/test')
def hello_world():
    return 'Hello, World!'

@app.cli.command("migrate-db")
@click.option('--path', help='folder path')
def migrate_db(path):
    migrate(path)


@app.cli.command("fix-stars-count")
def fix_stars_count():
    total_per_paper = db.session.query(paper_collection_table.c.paper_id, func.count(
        paper_collection_table.c.collection_id)).group_by(paper_collection_table.c.paper_id).all()
    with_stars = [p for p in total_per_paper if p[1] > 0]
    id_to_stars = {p[0]: p[1] for p in with_stars}
    papers = Paper.query.filter(Paper.id.in_(list(id_to_stars.keys()))).all()
    for p in papers:
        p.num_stars = id_to_stars[p.id]
    db.session.commit()
