import pymongo
import requests
import csv
from io import StringIO
import logging
from dotenv import load_dotenv
import os

logger = logging.getLogger(__name__)

client = pymongo.MongoClient()
mdb = client.arxiv
papers = mdb.papers

load_dotenv()


def fetch_data():
    user = os.environ.get('PAPERSWITHCODE_USER')
    password = os.environ.get('PAPERSWITHCODE_PASS')
    response = requests.get('https://paperswithcode.com/api/linkstars', auth=(user, password))
    content = response.content.decode('utf-8')
    f = StringIO(content)
    data = csv.DictReader(f, escapechar='\\')
    return data


def update_db(data):
    for row in data:
        cur_id = {'_id': row['arxiv_id']}
        {'github_link': 'https://github.com/Novemser/deep-imitation', 'high_conf': 'False', 'proceeding': '',
         'datasets': '', 'stars': '4', 'framework': 'pytorch',
         'tasks': 'Image-to-Image Translation|Video Generation|Image-To-Image Translation|Face Generation'}

        if papers.find(cur_id).count() > 0:
            obj = {
                'github_link': row.get('github_link'),
                'extraction_conf': row.get('high_conf', '') == 'True',
                'conferences': row.get('proceeding', ''),
                'stars': int(row.get('stars', 0)),
                'framework': row.get('framework'),
                'datasets': row.get('datasets', '').split('|'),
                'tasks': row.get('tasks', '').split('|')
            }
            try:
                papers.update(cur_id, {'$set': {'code': obj}})
            except Exception as e:
                logger.error('Failed to update paper {} - {}'.format(cur_id['_id'], e))
        else:
            logger.info('Paper not found - {}'.format(cur_id['_id']))


def fetch_code_data():
    logger.info('Fetching data from papers with code')
    data = fetch_data()
    logger.info('Updating DB with data from papers with code')
    update_db(data)
    logger.info('Finished updating data from papers with code')


if __name__ == "__main__":
    from logger import logger_config
    logger_config(info_filename='arxiv_fetcher.log')
    fetch_code_data()

