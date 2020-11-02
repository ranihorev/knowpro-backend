import logging
import os
import re
import dateparser
import xml.etree.ElementTree as ET
from datetime import datetime
from typing import Any, Dict, List, Tuple, NamedTuple
from flask_restful import marshal

import requests
from diskcache import Cache
from flask_socketio import emit
from sqlalchemy.orm.exc import NoResultFound

from ..models import Author, MetadataState, Paper, db
from .file_utils import FileUploader
from .paper_query_utils import metadata_fields

cache = Cache('cache')
logger = logging.getLogger(__name__)


class AuthorObj(NamedTuple):
    first_name: str
    last_name: str
    org: List[str]

    def get_name(self):
        return f'{self.first_name} {self.last_name}'


def get_tag_text(tree, tag, default_value='') -> str:
    return getattr(tree.find(f'.//{tag}'), 'text', default_value)


def get_all_tag_texts(tree, tag):
    element = tree.findall(f'.//{tag}')
    return [e.text for e in element]


def parse_coordinates(elem: ET.Element):
    boxes_raw = elem.get('coords', '').split(';')
    bounding_boxes = []
    for box_raw in boxes_raw:
        page, x, y, h, w = box_raw.split(',')
        bounding_boxes.append(dict(page=int(page), x=float(x), y=float(y), h=float(h), w=float(w)))
    return bounding_boxes


def get_table_of_contents(tree: ET.Element):
    content_tags = ['head', 'figure']
    all_coord_elements = tree.findall('.//*[@coords]')
    elements = []
    for elem in all_coord_elements:
        tag = elem.tag
        if tag not in content_tags:
            continue
        text = elem.text
        if tag == 'figure':
            tag = elem.get('type', tag)  # Get more accurate tag
            figure_head = getattr(elem.find('.//head'), 'text', '')
            figure_desc = getattr(elem.find('.//figDesc'), 'text', '')
            if figure_head.replace(' ', '') in figure_desc.replace(' ', ''):
                figure_head = ''
            text = ' - '.join(filter(None, [figure_head, figure_desc]))

        elements.append(dict(tag=tag, text=text, coordinates=parse_coordinates(elem)))

    return elements


def fetch_data_from_grobid(file_content) -> Tuple[bool, Dict[str, Any]]:
    try:
        grobid_url = os.environ.get('GROBID_URL')
        if not grobid_url:
            raise KeyError('Grobid URL is missing')
        grobid_res = requests.post(grobid_url + '/api/processFulltextDocument',
                                   data={'consolidateHeader': 1, 'teiCoordinates': ['head', 'figure']}, files={'input': file_content})
        if grobid_res.status_code == 503:
            raise Exception('Grobid is unavailable')
        content = re.sub(' xmlns="[^"]+"', '', grobid_res.text)
        tree: ET.Element = ET.fromstring(content)
    except Exception as e:
        logger.error(f'Failed to extract metadata for paper - {e}')
        return False, {'title': 'Untitled', 'authors': [], 'abstract': '', 'date': datetime.now()}

    header = tree.find('.//teiHeader')
    title = get_tag_text(header, 'title')

    authors_tree = header.findall('.//author')

    try:
        table_of_contents = get_table_of_contents(tree)
    except Exception as e:
        logger.error(f'Failed to extract table of contents - {e}')
        table_of_contents = []

    authors: List[AuthorObj] = []
    for author_tree in authors_tree:
        author = AuthorObj(first_name=get_tag_text(author_tree, 'forename'),
                           last_name=get_tag_text(author_tree, 'surname'),
                           org=get_all_tag_texts(author_tree, 'orgName'))
        authors.append(author)
    abstract_node = header.find('.//abstract')
    abstract = ''
    if abstract_node:
        abstract = ' '.join([n.strip() for n in abstract_node.itertext()]).strip()

    doi = getattr(header.find(".//idno[@type='DOI']"), 'text', None)
    publish_date = None
    publish_date_raw = header.find('.//date')
    if publish_date_raw is not None:
        try:
            publish_date = dateparser.parse(publish_date_raw.get('when'))
        except Exception as e:
            logger.error(f'Failed to extract date for {publish_date_raw.text}')

    return True, {'title': title or None, 'authors': authors, 'abstract': abstract, 'date': publish_date, 'doi': doi, 'table_of_contents': table_of_contents}


def extract_paper_metadata(paper_id: str):
    paper: Paper = Paper.query.get_or_404(paper_id)
    paper.metadata_state = MetadataState.fetching  # TODO: move this to redis
    db.session.commit()
    file_content = requests.get(paper.local_pdf).content
    file_hash = FileUploader.calc_hash(file_content)
    metadata, _ = cache.get(file_hash, expire_time=True)
    if not metadata:
        logger.info(f'Fetching data from grobid for paper - {paper_id}')
        success, metadata = fetch_data_from_grobid(file_content)
        if success:
            logger.info(f'Fetched data from grobid! - {paper_id}')
            cache.set(file_hash, metadata, expire=24 * 60 * 60)
        else:
            emit('paperInfo', {'success': False}, namespace='/', room=str(paper.id))
            return

    paper.title = metadata.get('title', paper.title)
    paper.abstract = metadata.get('abstract', paper.abstract)
    paper.publication_date = metadata.get('date', paper.publication_date)
    paper.doi = metadata.get('doi', paper.doi)
    paper.table_of_contents = metadata.get('table_of_contents', paper.table_of_contents)

    # Create authors
    for current_author in metadata['authors']:
        try:
            author = Author.query.filter(
                Author.first_name == current_author.first_name, Author.last_name == current_author.last_name).one()
        except NoResultFound:
            author = Author(name=f'{current_author.first_name} {current_author.last_name}',
                            first_name=current_author.first_name, last_name=current_author.last_name, organization=current_author.org)
            db.session.add(author)
        author.papers.append(paper)

    paper.last_update_date = datetime.now()
    paper.metadata_state = MetadataState.ready
    db.session.commit()
    emit('paperInfo', {'success': True, 'data': marshal(paper, metadata_fields)}, namespace='/', room=str(paper.id))
