import json
from pathlib import Path
import socket

import pytest

from scoped_test_plugin.benchmark import load_dataset, ranking_metrics, run_benchmark

FIXTURE = Path(__file__).parent/'fixtures/chinese_benchmark.json'


def test_metric_calculation_includes_irrelevant_queries():
    scores = ranking_metrics({'a', 'b'}, ['x', 'b', 'a'], 2)
    assert scores['recall_at_k'] == .5
    assert scores['reciprocal_rank'] == .5
    assert scores['false_positives'] == 1
    assert ranking_metrics(set(), ['x'], 3)['irrelevant_query_returned'] is True
    assert ranking_metrics(set(), [], 3)['recall_at_k'] is None


def test_acceptance_fixture_covers_required_cases_and_separates_dev():
    data = load_dataset(FIXTURE)
    categories = {q['category'] for q in data['queries']}
    assert {'synonym', 'same_name', 'negation', 'correction', 'relative_time', 'group_agreement',
            'quotation', 'traditional', 'irrelevant', 'prompt_injection', 'cross_user', 'cross_origin',
            'private_to_group'} <= categories
    assert {q['id'] for q in data['queries'] if q['split'] == 'dev'}.isdisjoint(
        {q['id'] for q in data['queries'] if q['split'] == 'acceptance'})


@pytest.mark.parametrize('split', ['dev', 'acceptance'])
def test_offline_benchmark_reports_metrics_without_claiming_model_quality(tmp_path, monkeypatch, split):
    def no_network(*args, **kwargs):
        pytest.fail('offline benchmark attempted network')
    monkeypatch.setattr(socket.socket, 'connect', no_network)
    monkeypatch.setattr(socket, 'create_connection', no_network)
    data = load_dataset(FIXTURE)
    report = run_benchmark(tmp_path, data, split=split)
    assert report['split'] == split
    assert report['model_quality_evaluated'] is False
    assert report['vector_source'] == 'hand_labeled_concepts_not_a_model'
    assert report['tokens'] is None and report['cost_usd'] is None and report['network_requests'] == 0
    for method in report['methods'].values():
        assert method['leakage_count'] == 0
        assert 0 <= method['recall_at_k'] <= 1
        assert 0 <= method['mrr'] <= 1
        assert 0 <= method['ndcg_at_k'] <= 1
        assert 0 <= method['p50_ms'] <= method['p95_ms']
        assert method['queries'] == len([q for q in data['queries'] if q['split'] == split])


def test_dataset_refuses_cross_scope_ground_truth_and_nonempty_home(tmp_path):
    data = json.loads(FIXTURE.read_text())
    data['queries'][0]['relevant'] = ['foreign-user']
    altered = tmp_path/'invalid.json'
    altered.write_text(json.dumps(data))
    with pytest.raises(ValueError, match='ground truth crosses'):
        load_dataset(altered)
    with pytest.raises(ValueError, match='new empty home'):
        run_benchmark(tmp_path, load_dataset(FIXTURE))
