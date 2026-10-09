"""A small test for "is this question English", for ``[index] bm25 = "english"``.

The keyword (BM25) ranking helps on English questions over an English corpus and adds noise
for other languages, whose words rarely occur in the chunks. The index sees only the question,
so this counts function words: a question is English when it has more English ones than
words of Spanish, French, German, Portuguese or Italian, or none of either and only ASCII
letters (a bare keyword query over an English corpus). Not a language identifier; it only has
to tell the questions of this project apart.
"""

from __future__ import annotations

import re


def _words(text: str) -> frozenset[str]:
    return frozenset(text.split())


ENGLISH = _words(
    "the of and is are was were be been what which who whom whose how why when where does do "
    "did can could should would will with without for from this that these those it its in on "
    "at by as than then there their they we our you your not or if between about into over "
    "under has have had"
)
# Words that are not also English words (a, me, no, son, come, sin, are left out).
OTHER = _words(
    "el la los las un una unos unas de del que qué es son por para con como cómo cuál cuáles "
    "cuándo dónde quién quiénes se su sus al lo y o pero más muy entre sobre desde hasta hay "
    "está están ser fue "
    "le les des du est sont pour dans avec comment quel quelle quels quelles qui quoi pourquoi "
    "quand où et ou mais plus très sur sous entre être "
    "der die das den dem des ein eine einer einen und ist sind für mit von zu im wie was "
    "welche welcher warum wann wo nicht auch auf aus bei "
    "uma uns umas dos das não são você como qual quais porque quando onde "
    "il gli lo della dello degli delle che cosa quale quali perché dove sono è con"
)
_WORD = re.compile(r"[^\W\d_]+", re.UNICODE)


def looks_english(question: str) -> bool:
    words = [w.lower() for w in _WORD.findall(question)]
    english = sum(w in ENGLISH for w in words)
    other = sum(w in OTHER for w in words)
    if english or other:
        return english > other
    return all(w.isascii() for w in words)
