"""Versioned local scoring; not an upstream/official SearchQA evaluator."""
import hashlib
import html
import json
import re
import string
import unicodedata
from collections import Counter

LEGACY = 'normalized-em-token-f1.v1'
SURFACE = 'searchqa.surface-alias.v2'


def normalized(text):
    text = ''.join(c for c in text.lower() if c not in string.punctuation)
    return ' '.join(re.sub(r'\b(a|an|the)\b', ' ', text).split())


def surface(text):
    text = unicodedata.normalize('NFKC', html.unescape(text)).casefold()
    text = text.replace("\\'", "'").translate(str.maketrans({'’': "'", '‘': "'", '“': '"', '”': '"'}))
    # Word separators are distinct from a numeric minus sign. Keep mathematical
    # punctuation so 1.2/12, -1/1 and C++/C cannot become exact matches.
    text = re.sub(r'(?<=\w)[\-‐‑–—](?=[^\W\d_])', ' ', text)
    text = re.sub(r"(?<=\w)'(?=\w)", '', text)
    text = re.sub(r'["!,;?:()\[\]{}]', ' ', text)
    text = re.sub(r'(?<!\d)\.|\.(?!\d)', ' ', text)
    text = ' '.join(text.split()).strip("' ")
    without_articles = ' '.join(re.sub(r'\b(a|an|the)\b', ' ', text).split())
    return without_articles or text  # a single-letter answer A must not equal an empty answer


def aliases(target):
    values = [(target, 'declared')]
    # Only an explicit, non-nested trailing alias or narrow explanatory note.
    alternative = re.fullmatch(r'\s*([^()]+?)\s*\(or\s+([^()]+)\)\s*', target, re.I)
    annotation = re.fullmatch(r'\s*([^()]+?)\s*\(for (?:her|his|their) (?:role|performance|work)\b[^()]*\)\s*', target, re.I)
    if alternative:
        values.extend((s.strip(), 'explicit_gold_alternative') for s in alternative.groups())
    elif annotation:
        values.append((annotation[1].strip(), 'gold_explanatory_note'))
    return values


def score_answer(answer, gold, version=LEGACY):
    if version not in (LEGACY, SURFACE):
        raise ValueError('Unknown SearchQA scorer: ' + str(version))
    norm = normalized if version == LEGACY else surface
    prediction = norm(answer); pt = prediction.split(); scores = []
    for target in gold:
        for alias, rule in aliases(target) if version == SURFACE else [(target, 'declared')]:
            truth = norm(alias); tt = truth.split()
            shared = sum((Counter(pt) & Counter(tt)).values())
            f1 = 2 * shared / (len(pt) + len(tt)) if pt and tt else float(pt == tt)
            if version == SURFACE and not prediction:f1 = 0.0
            scores.append((float(prediction == truth and (version == LEGACY or bool(prediction))), f1, rule))
    out = {'em': max(x[0] for x in scores), 'f1': max(x[1] for x in scores)}
    if version == SURFACE:
        legacy = score_answer(answer, gold, LEGACY)
        out.update(scorer_version=version, match_rule=max(scores, key=lambda x: (x[0], x[1]))[2],
                   legacy_em=legacy['em'], legacy_f1=legacy['f1'])
    return out


def contract(version=SURFACE):
    if version not in (LEGACY, SURFACE):
        raise ValueError('Unknown SearchQA scorer: ' + str(version))
    row = {'version': version, 'label': 'Local surface/explicit-alias EM + token F1' if version == SURFACE else 'Legacy normalized EM + token F1',
           'official': False, 'gold_visible_to_agent': False}
    row['sha256'] = hashlib.sha256(json.dumps(row, sort_keys=True).encode()).hexdigest()
    return row


def selected_version(selection):
    row = selection.get('evaluation')
    if row is None:
        version = selection.get('dataset', {}).get('scorer', LEGACY)
        contract(version)
        return version
    if row != contract(row.get('version')):
        raise ValueError('Evaluation contract changed')
    return row['version']
