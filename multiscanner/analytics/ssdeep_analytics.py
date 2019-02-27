#!/usr/bin/env python

'''
Set of analytics based on ssdeep hash.

- compare
    Simple implementation of ssdeep comparisions using a few optimizations
    described at the links below

    https://www.virusbulletin.com/virusbulletin/2015/11/optimizing-ssdeep-use-scale
    http://www.intezer.com/intezer-community-tip-ssdeep-comparisons-with-elasticsearch/

    Designed to be run on a regular basis (e.g., nightly).

    For each sample that has not run ssdeep analytic, search for samples where
    ssdeep.compare > 0 based on chunksize, chunk 7grams, and double-chunk
    7grams. Update sample with any matches and mark ssdeep analytic as having
    run.

- group
    Returns SHA256 hashes of samples grouped based on ssdeep hash.
'''

import argparse
import json
import logging
import sys
from pprint import pformat

logger = logging.getLogger(__name__)

try:
    import ssdeep
except ImportError:
    logger.error("ssdeep module not installed...")
    ssdeep = None


from multiscanner.config import get_config_path, read_config
from multiscanner.storage import storage


class SSDeepAnalytic:

    def __init__(self, debug=False):
        storage_conf = get_config_path('storage')
        conf = read_config(storage_conf)
        storage_handler = storage.StorageHandler(configfile=storage_conf)
        es_handler = storage_handler.load_required_module('ElasticSearchStorage')

        if not es_handler:
            logger.error('This analytic only works with ES storage module.')
            sys.exit(0)

        # probably not ideal...
        self.es = es_handler.es
        self.index = conf['ElasticSearchStorage']['index']
        self.doc_type = '_doc'

        self.debug = debug

    def ssdeep_compare(self):
        if ssdeep is None:
            logger.error("ssdeep module not installed... can't perform ssdeep_compare()")
            return
        # get all of the samples where ssdeep_compare has not been run
        # e.g., ssdeepmeta.analyzed == false
        query = {
            '_source': ['ssdeep', 'SHA256'],
            'query': {
                'bool': {
                    'must': [
                        {'match': {'ssdeep.analyzed': 'false'}}
                    ]
                }
            }
        }

        page = self.es.search(
            self.index,
            scroll='2m',
            size=1000,
            body=query)

        records_list = []
        while len(page['hits']['hits']) > 0:
            for hit in page['hits']['hits']:
                records_list.append(hit)
            sid = page['_scroll_id']
            page = self.es.scroll(scroll_id=sid, scroll='2m')

        for new_ssdeep_hit in records_list:
            new_ssdeep_hit_src = new_ssdeep_hit.get('_source')
            chunksize = new_ssdeep_hit_src.get('ssdeep').get('chunksize')
            chunk = new_ssdeep_hit_src.get('ssdeep').get('chunk')
            double_chunk = new_ssdeep_hit_src.get('ssdeep').get('double_chunk')
            new_sha256 = new_ssdeep_hit_src.get('SHA256')

            # build new query for docs that match our optimizations
            # https://github.com/intezer/ssdeep-elastic/blob/master/ssdeep_elastic/ssdeep_querying.py#L35
            opti_query = {
                '_source': ['ssdeep', 'SHA256'],
                'query': {
                    'bool': {
                        'must': [
                            {
                                'terms': {
                                    'ssdeep.chunksize': [chunksize, chunksize / 2, chunksize * 2]
                                }
                            },
                            {
                                'bool': {
                                    'should': [
                                        {
                                            'match': {
                                                'ssdeep.chunk': {
                                                    'query': chunk
                                                }
                                            }
                                        },
                                        {
                                            'match': {
                                                'ssdeep.double_chunk': {
                                                    'query': double_chunk
                                                }
                                            }
                                        }
                                    ],
                                    'minimum_should_match': 1
                                }
                            },
                            {
                                'bool': {
                                    'must_not': {
                                        'match': {
                                            'SHA256': new_sha256
                                        }
                                    }
                                }
                            }
                        ]
                    }
                }
            }

            # this bool condition isn't working how I expect
            #   if we have already updated the match dictionary to
            #   include a hit, don't rerun it for the inverse
            #                 {
            #                      'bool': {
            #                          'must_not': {
            #                              'exists': {
            #                                  'field': 'ssdeep.matches.' + new_sha256
            #                              }
            #                          }
            #                      }
            #                 }

            opti_page = self.es.search(
                self.index,
                scroll='2m',
                size=1000,
                body=opti_query)

            while len(opti_page['hits']['hits']) > 0:
                # for each hit, ssdeep.compare != 0; update the matches
                for opti_hit in opti_page['hits']['hits']:
                    opti_hit_src = opti_hit.get('_source')
                    opti_sha256 = opti_hit_src.get('SHA256')
                    result = ssdeep.compare(
                                new_ssdeep_hit_src.get('ssdeep').get('ssdeep_hash'),
                                opti_hit_src.get('ssdeep').get('ssdeep_hash'))

                    if self.debug:
                        logger.debug(new_ssdeep_hit_src.get('SHA256'))
                        logger.debug(opti_hit_src.get('SHA256'))
                        logger.debug(result)

                    msg = {'doc': {'ssdeep': {'matches': {opti_sha256: result}}}}
                    self.es.update(
                        index=self.index,
                        doc_type=self.doc_type,
                        id=new_ssdeep_hit.get('_id'),
                        body=json.dumps(msg))

                    msg = {'doc': {'ssdeep': {'matches': {new_sha256: result}}}}
                    self.es.update(
                        index=self.index,
                        doc_type=self.doc_type,
                        id=opti_hit.get('_id'),
                        body=json.dumps(msg))

                    opti_sid = opti_page['_scroll_id']
                    opti_page = self.es.scroll(scroll_id=opti_sid, scroll='2m')

            # analytic has run against sample, set ssdeep.analyzed = true
            msg = {'doc': {'ssdeep': {'analyzed': 'true'}}}
            self.es.update(
                index=self.index,
                doc_type=self.doc_type,
                id=new_ssdeep_hit.get('_id'),
                body=json.dumps(msg))

    def ssdeep_group(self):
        # get all of the samples where ssdeep_compare has not been run
        # e.g., ssdeepmeta.analyzed == false
        query = {
            '_source': ['ssdeep', 'SHA256'],
            'query': {
                'exists': {
                    'field': 'ssdeep.matches'
                }
            }
        }

        page = self.es.search(
            self.index,
            scroll='2m',
            size=1000,
            body=query)

        records = {}
        while len(page['hits']['hits']) > 0:
            for hit in page['hits']['hits']:
                hit_src = hit.get('_source')
                records[hit_src.get('SHA256')] = hit_src.get('ssdeep', {}) \
                                                        .get('matches', {})
            sid = page['_scroll_id']
            page = self.es.scroll(scroll_id=sid, scroll='2m')

        # inspired by ssdc
        groups = []
        for sha256_, matches_dict in records.items():
            in_group = False
            for i in range(len(groups)):
                if sha256_ in groups[i]:
                    in_group = True
                    continue
                should_add = True
                for match_hash in groups[i]:
                    if match_hash not in records.get(sha256_):
                        should_add = False
                if should_add:
                    groups[i].append(sha256_)
                    in_group = True
            if not in_group:
                groups.append([sha256_])

        return groups


def main():
    parser = argparse.ArgumentParser(description='Script to interact with '
        'Multiscanner\'s Elasticsearch datastore to run analytics based on '
        'ssdeep hash.')
    group = parser.add_mutually_exclusive_group(required=True)
    parser.add_argument('-v', '--verbose', dest='verbose', action='store_true',
                        help='Increase output to stdout')
    group.add_argument('-c', '--compare', dest='compare', action='store_true',
        help='Run ssdeep.compare using a few optimizations based on ssdeep'
        ' hash structure.')
    group.add_argument('-g', '--group', dest='group', action='store_true',
        help='Returns group of samples based on ssdeep hash.')

    args = parser.parse_args()

    ssdeep_analytic = SSDeepAnalytic(debug=args.verbose)

    if args.compare:
        ssdeep_analytic.ssdeep_compare()
        logger.info('[*] Success')
    elif args.group:
        logger.info(pformat(ssdeep_analytic.ssdeep_group()))
        logger.info('[*] Success')


if __name__ == '__main__':
    main()
