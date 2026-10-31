"""Offline retrieval harness; hand-labelled vectors are not model evaluation.

Run against the checked-in fixture and a new temporary home. No key, model,
network transport or production profile is accessed by this module.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
from pathlib import Path
import statistics
import sys
import tempfile
import time
from types import ModuleType

if __package__:
    from .retrieval import Fingerprint, HybridRetriever, VectorIndex
    from .scope import ScopeContext
    from .store import Store
else:
    # Allow direct CLI execution despite the plugin directory's hyphen.
    root = Path(__file__).resolve().parent
    # A namespace avoids importing the provider and its Hermes runtime. The
    # benchmark can run with standard-library Python and a completely empty home.
    module = ModuleType('scoped_benchmark_plugin')
    module.__path__ = [str(root)]
    sys.modules[module.__name__] = module
    from scoped_benchmark_plugin.retrieval import Fingerprint, HybridRetriever, VectorIndex
    from scoped_benchmark_plugin.scope import ScopeContext
    from scoped_benchmark_plugin.store import Store


def load_dataset(path):
    raw = Path(path).read_bytes()
    data = json.loads(raw)
    if (not isinstance(data, dict) or data.get('schema_version') != 1
            or data.get('vector_source') != 'hand_labeled_concepts_not_a_model'):
        raise ValueError('unsupported benchmark dataset')
    concepts = data.get('concepts')
    if not isinstance(concepts, list) or not concepts or len(concepts) != len(set(concepts)):
        raise ValueError('invalid benchmark concepts')
    documents, queries = data.get('documents'), data.get('queries')
    if not isinstance(documents, list) or not isinstance(queries, list) or not documents or not queries:
        raise ValueError('empty benchmark dataset')
    ids = set()
    for record in documents+queries:
        if not isinstance(record, dict) or record.get('split') not in ('dev', 'acceptance'):
            raise ValueError('invalid benchmark split')
        if not isinstance(record.get('id'), str) or record['id'] in ids:
            raise ValueError('duplicate benchmark identity')
        ids.add(record['id'])
        for name in ('domain', 'principal', 'origin'):
            if not isinstance(record.get(name), str) or not record[name]:
                raise ValueError('invalid benchmark scope')
        if not isinstance(record.get('concepts'), list) or not set(record['concepts']) <= set(concepts):
            raise ValueError('invalid benchmark annotation')
    by_id = {row['id']: row for row in documents}
    for query in queries:
        if not isinstance(query.get('relevant'), list) or not set(query['relevant']) <= by_id.keys():
            raise ValueError('unknown benchmark ground truth')
        for ident in query['relevant']:
            document = by_id[ident]
            if any(query[key] != document[key] for key in ('domain', 'principal', 'origin')):
                raise ValueError('ground truth crosses permission scope')
    data['sha256'] = hashlib.sha256(raw).hexdigest()
    return data


def ranking_metrics(relevant, returned, k):
    relevant, returned = set(relevant), list(dict.fromkeys(returned))[:k]
    hits = [1 if ident in relevant else 0 for ident in returned]
    dcg = sum(hit/math.log2(rank+2) for rank, hit in enumerate(hits))
    ideal = sum(1/math.log2(rank+2) for rank in range(min(k, len(relevant))))
    return {
        'recall_at_k': sum(hits)/len(relevant) if relevant else None,
        'reciprocal_rank': next((1/(rank+1) for rank, hit in enumerate(hits) if hit), 0) if relevant else None,
        'ndcg_at_k': dcg/ideal if ideal else None,
        'false_positives': sum(1 for ident in returned if ident not in relevant),
        'irrelevant_query_returned': bool(returned) if not relevant else None,
    }


def _scope(record):
    return ScopeContext(record['domain'], 'napcat', record['principal'],
                        'dm' if record['origin'].startswith('dm:') else 'group', record['origin'])


def _vector(record, concepts):
    selected = set(record['concepts'])
    value = [1 if concept in selected else 0 for concept in concepts]
    if not any(value):
        raise ValueError('empty synthetic vector annotation')
    return value


def run_benchmark(home, dataset, *, split='acceptance', k=3):
    """Build only synthetic temporary data; report observed harness outcomes.

    Development and fixed acceptance queries are reported separately. The
    annotated vector path tests ranking/permissions, not Chinese model quality.
    """
    if split not in ('dev', 'acceptance') or type(k) is not int or not 1 <= k <= 20:
        raise ValueError('invalid benchmark split or k')
    home = Path(home)
    if any(home.iterdir()):
        raise ValueError('benchmark requires a new empty home')
    stores, indexes, identities = {}, {}, {}
    fingerprint = Fingerprint('synthetic-hand-labelled-concepts', 'offline-fixture',
                              len(dataset['concepts']), model_revision=dataset['sha256'])
    for document in dataset['documents']:
        domain = document['domain']
        if domain not in stores:
            stores[domain] = Store(home, domain)
            indexes[domain] = VectorIndex(stores[domain])
            indexes[domain].create_generation('synthetic-v1', fingerprint)
        scope = _scope(document)
        memory = stores[domain].add(scope, document['content'])
        identities[memory['id']] = document
        if document.get('state') == 'superseded':
            stores[domain].mutate(scope, memory['id'], 'archived')
        else:
            indexes[domain].put(scope, memory['id'], 'synthetic-v1', _vector(document, dataset['concepts']),
                                 vector_fingerprint=fingerprint)
    for index in indexes.values():
        index.activate('synthetic-v1')
    report = {'dataset': dataset['name'], 'dataset_sha256': dataset['sha256'], 'split': split, 'k': k,
              'vector_source': dataset['vector_source'], 'model_quality_evaluated': False,
              'network_requests': 0, 'tokens': None, 'cost_usd': None, 'methods': {}}
    for method in ('lexical', 'synthetic_hybrid'):
        cases, timings, leakage = [], [], 0
        for query in [q for q in dataset['queries'] if q['split'] == split]:
            store, index, scope = stores[query['domain']], indexes[query['domain']], _scope(query)
            start = time.perf_counter()
            if method == 'lexical':
                rows = store.search(scope, query['query'], k)
            else:
                rows = HybridRetriever(store, index).search(scope, query['query'],
                    _vector(query, dataset['concepts']), limit=k, query_fingerprint=fingerprint)
            timings.append((time.perf_counter()-start)*1000)
            retrieved = [identities[row['id']]['id'] for row in rows]
            leaks = sum(any(identities[row['id']][field] != query[field]
                            for field in ('domain', 'principal', 'origin')) for row in rows)
            leakage += leaks
            cases.append({'id': query['id'], 'category': query['category'], 'returned': retrieved,
                          'leakage_count': leaks, **ranking_metrics(query['relevant'], retrieved, k)})
        if not cases:
            raise ValueError('requested benchmark split is empty')
        positives = [case for case in cases if case['recall_at_k'] is not None]
        negatives = [case for case in cases if case['irrelevant_query_returned'] is not None]
        report['methods'][method] = {
            'queries': len(cases), 'positive_queries': len(positives), 'irrelevant_queries': len(negatives),
            'recall_at_k': statistics.mean(c['recall_at_k'] for c in positives) if positives else None,
            'mrr': statistics.mean(c['reciprocal_rank'] for c in positives) if positives else None,
            'ndcg_at_k': statistics.mean(c['ndcg_at_k'] for c in positives) if positives else None,
            'false_positive_count': sum(c['false_positives'] for c in cases),
            'irrelevant_query_return_rate': statistics.mean(c['irrelevant_query_returned'] for c in negatives) if negatives else None,
            'leakage_count': leakage, 'p50_ms': statistics.median(timings),
            'p95_ms': sorted(timings)[max(0, math.ceil(.95*len(timings))-1)], 'cases': cases,
        }
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--dataset', type=Path, default=Path(__file__).parent/'tests/fixtures/chinese_benchmark.json')
    parser.add_argument('--split', choices=('dev', 'acceptance'), default='acceptance')
    parser.add_argument('--k', type=int, default=3)
    args = parser.parse_args()
    dataset = load_dataset(args.dataset)
    with tempfile.TemporaryDirectory(prefix='scoped-memory-benchmark-') as home:
        print(json.dumps(run_benchmark(home, dataset, split=args.split, k=args.k), ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
