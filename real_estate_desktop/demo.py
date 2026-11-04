from __future__ import annotations
import json

from . import fixtures

# Generated business fixtures: 20 communities with six topic documents each,
# 200 listings and 10 customers. The provenance flag stays technical; reports
# never label the content as sample data.


def property_records():
    return fixtures.listing_rows()


def seed_demo(settings, business, memory, knowledge):
    marker = settings.data_dir / '.demo_initialized'
    if marker.exists():
        return
    business.put_properties(fixtures.listing_rows())
    for cid, name, notes, preferences in fixtures.CUSTOMERS:
        try:
            business.customer(settings.broker_id, cid)
        except PermissionError:
            business.create_customer(settings.broker_id, name, notes, True, cid)
        if preferences:
            memory.apply(settings.broker_id, cid, preferences, '客户初始偏好', 'fixture-initial-' + cid)
    catalogue = {row['key']: row for row in fixtures.community_rows()}
    metadata = knowledge.metadata()
    for filename, extra, text in fixtures.topic_documents():
        community = catalogue[filename.split('-', 1)[0]]
        (settings.knowledge_dir / filename).write_text(text, 'utf-8')
        metadata[filename] = dict(title=extra['title'], section=extra['section'], community_key=community['key'],
                                  region=community['region'], source='', as_of='2026-10-30', is_demo=True)
    (settings.knowledge_dir / 'metadata.json').write_text(json.dumps(metadata, ensure_ascii=False, indent=2), 'utf-8')
    knowledge.sync()
    marker.write_text('Generated fixtures initialized.', 'utf-8')
