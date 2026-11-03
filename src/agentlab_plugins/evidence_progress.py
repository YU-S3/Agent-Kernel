"""Per-consumer evidence coverage, independent of retrieval IDs and pagination."""
import hashlib


class EvidenceProgress:
    def __init__(self):
        self.spans = {}
        self.titles = set()
        self.ids = set()

    def observe(self, snippet, start, end):
        text = snippet['text']
        if type(start) is not int or not 0 <= start <= end <= len(text):
            raise ValueError('Invalid evidence span')
        key = hashlib.sha256(text.encode('utf-8')).hexdigest()
        spans = self.spans.setdefault(key, [])
        overlap = sum(max(0, min(end, b) - max(start, a)) for a, b in spans)
        new_chars = end - start - overlap
        merged = []
        for a, b in sorted([*spans, (start, end)]):
            if merged and a <= merged[-1][1]:
                merged[-1] = (merged[-1][0], max(b, merged[-1][1]))
            else:
                merged.append((a, b))
        self.spans[key] = merged
        title = snippet['title'][:160]
        new_title_chars = len(title) if title not in self.titles else 0
        self.titles.add(title)
        self.ids.add(snippet['id'])
        return {'new_count': int(new_chars + new_title_chars > 0),
                'new_chars': new_chars, 'new_title_chars': new_title_chars,
                'novelty_basis': 'visible-text-span.v1'}
