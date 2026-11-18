"""Stable experiment version and participant naming, separate from Git commits."""

STAGES = {'pilot': (0, '预试'), 'formal': (1, '正式')}

# Feature-only releases share a content revision and its participant counter.
# Increment this for maintenance releases; publishing materials allocates version_no.
FEATURE_VERSION = 1


def release_info(version):
    return {'release_code': f"{version['version_code']}.{FEATURE_VERSION}",
            'content_version_code': version['version_code'], 'feature_version': FEATURE_VERSION}


def version_info(mode, number):
    stage, label = STAGES[mode]
    code = f'{stage}.{number}'
    return {'stage': stage, 'stage_name': label, 'version_no': number, 'version_code': code,
            'batch_id': f'{mode}-v{code}', 'data_directory': f'batches/{mode}/v{code}'}


def participant_code(mode, version_no, number, quick=False):
    stage = STAGES[mode][0]
    return ('Q-' if quick else '') + f'S{stage}-V{version_no:04d}-P{number:06d}'
