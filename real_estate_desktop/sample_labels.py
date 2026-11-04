"""One-time migration helpers for data created by earlier fixture versions."""
from __future__ import annotations

import json
import uuid


def migrate_sample_labels(settings, business, memory, knowledge):
    """Older packages seeded one overview file per community.

    The current fixtures write topic files that already carry a ``section``
    field, so this migration only backfills the missing section for legacy
    documents and never rewrites files that have one.
    """
    marker = settings.data_dir / '.sample_content_v3'
    if marker.exists():
        return
    metadata = knowledge.metadata()
    changed = False
    for filename, row in list(metadata.items()):
        if not isinstance(row, dict) or row.get('section'):
            continue
        title = str(row.get('title') or '')
        row['section'] = '概况' if title.endswith('概况') else '资料'
        metadata[filename] = row
        changed = True
    if changed:
        temporary = knowledge.folder / ('metadata-labels-' + uuid.uuid4().hex + '.tmp')
        temporary.write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding='utf-8')
        temporary.replace(knowledge.folder / 'metadata.json')
        knowledge.sync()
    marker.write_text('Fixture metadata version 3; sections backfilled.', encoding='utf-8')
