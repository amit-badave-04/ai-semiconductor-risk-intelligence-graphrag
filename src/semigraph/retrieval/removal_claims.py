"""Removal claims: which sentences of an answer say a risk disclosure went away, and whether their citations support that.

Split out of ``verify.py`` (which keeps the numeric, citation and refusal checks and the ``AnswerChecks`` object): this part reads
only ``context_layout`` (the labels the context prints and ``removal_supported_ids``) and ``ids``, so it stays a pure function of
``(answer text, supported ids)``. Its public entry point is :func:`unsupported_removal_claims`.

A sentence that says a risk disclosure was dropped / removed / "no longer appears" (or that its wording "was not found in the newer
filing", the hedge the answer prompt teaches for a removed passage) may cite only ids listed under the temporal block's REMOVED
lists. What the answer prompt itself tells the model to write is not a claim: a NEGATED or "none found" statement ("not evidence that
any risk factor was dropped", "the text check found no risk factor that no longer appears", "the check cannot tell whether it was
removed") says the disclosure survives, and a list LABEL copied from the context (a heading, the quoted label) names a list, not a
removal; the items written UNDER a removed-list label are judged by their ids exactly like a claim. Negation reaches a bounded
distance ahead of the verb and is ended by contrast words, a comma that opens a new statement, an "and" before the verb (so "did
not add new risks and removed X" is still a claim) and the exceptions listed at ``_NEGATORS``.

Size: about 1,050 lines, over the 800-line soft ceiling on purpose: the language half (what counts as a removal claim and what
negates it) and the structure half (headings, bullets, copied labels, the sentence loop) share seventeen private patterns, so
splitting them would trade one cohesive module for a wall of cross-imports.

Known limits (a heuristic, not a parser; 2026-09-27 adversarial review, about 1,100 crafted sentences over three rounds): a POSITIVE claim built around
an earlier unrelated negation ("Without warning Nvidia removed X", "None of this changes the fact that Nvidia removed X") can slip
through, and some honest sentences that mix a negation with a hedge word ("Far from being removed, ... was expanded") are still
flagged. Vocabulary gaps (missed by the version before this module too): "is absent from", "is missing from", "is gone from", "cut", "took out",
"was not carried over", "did not survive into", "did not retain", "does not contain ... any more" are not recognised as removal wording at all. The check is a safety net against a model that ignores the hedged wording, not against an adversary; the prompt, the
context labels and the judge are the other layers.
"""

import re

from .context_layout import (
    PASSAGES_PREFIX,
    PASSAGES_REMOVED_PHRASE,
    REMOVED_ITEMS_PREFIX,
    TEMPORAL_HEADER,
    UNSETTLED_ITEMS_PREFIX,
    removal_supported_ids,
)
from .ids import CITE_RE
from .textutil import A as _A
from .textutil import plain_spaces as _plain_spaces


# --- removal claims --------------------------------------------------------------------------------------------------

# A clause that says a disclosure went away: "dropped the risk", "no longer appears", "removed from the 10-K". The verb
# alone is not enough ("revenue dropped 5%" claims nothing about a disclosure), so it must also speak of a disclosure.
# The alternatives after ``stopped ...`` are the hedged wording the answer prompt teaches for a REMOVED item or passage ("its
# wording was not found in the newer filing", "the text check found no matching text", "does not appear in the newer
# filing"): they say the text is missing from the NEWER filing, so they need the same removed-list citation as "no longer
# appears". The direction matters. "Not found in the OLDER / earlier filing" is the hedge of an ADDED passage and "no matching
# risk factor found in the earlier filing" that of a NEW item: neither claims a removal, so a clause that places the search in
# the older filing is never a match. A filing named otherwise ("the 10-K for the fiscal year ended January 25, 2026", "the
# latest 10-K") has no known direction and counts as a claim: an added-passage hedge worded that way is escalated for
# nothing, the safe side. A refusal ("the figure was not found in the filings": no wording / sentence / risk subject) is not
# matched at all.
_NEWER_FILING = r"(?:the\s+)?(?:newer|later|more\s+recent)\b"
_NOT_IN_OLDER = r"(?![^.;\[\]]{0,80}?\bin\s+(?:the\s+)?(?:older|earlier|prior|previous)\b)"
_NOT_FOUND = r"(?:not|\w+n['’]t)\s+(?:be\s+)?found\b"
_MISSING_WORDING_SUBJECT = r"(?:wording|sentences?|passages?|statements?|language|paragraphs?|risk\s+factors?|risks?)"
_REMOVAL_RE = re.compile(
    r"\b(?:drop(?:s|ped|ping)?|remov(?:e|es|ed|ing|al|als)|no longer|eliminat(?:e|es|ed|ing|ion)|delet(?:e|es|ed|ing|ion)|"
    r"discontinu(?:e|es|ed|ing)|omit(?:s|ted|ting)?(?!\s+from\s+(?:the\s+|this\s+)?(?:context|list|excerpts?|results|answer|prompt))|"
    r"(?:stopped|ceased)\s+(?:to\s+)?(?:disclos|report|mention|includ|list)\w*|"
    rf"{_NOT_FOUND}\s+in\s+{_NEWER_FILING}|(?:does|do|did)(?:\s+not|n['’]t)\s+appear\s+in\s+{_NEWER_FILING}|"
    rf"{_MISSING_WORDING_SUBJECT}\b[^.;\[\]]{{0,60}}?\b{_NOT_FOUND}{_NOT_IN_OLDER}|"
    rf"no\s+match(?:ing|ed)\s+(?:text|wording|risk\s+factors?|paragraphs?|passages?|sentences?)\b[^.;\[\]]{{0,80}}?"
    rf"\bin\s+{_NEWER_FILING}|"
    rf"text\s+check\b[^.;\[\]]{{0,40}}?\bno\s+match(?:ing|ed)\s+(?:text|wording)\b{_NOT_IN_OLDER})\b",
    re.I)
_DISCLOSURE_RE = re.compile(
    r"\b(?:risks?|risk factors?|passages?|sentences?|paragraphs?|disclos\w*|wording|language|text|filings?|10-K|20-F|"
    r"item 1A|mention\w*|discussion|statements?|section|items?)\b", re.I)
_BRACKETED_RE = re.compile(r"\[[^\[\]\n]*\]")
# A removal verb that is NEGATED or QUANTIFIED BY NONE says the disclosure SURVIVES: "was reworded, not removed", "has not
# been removed", "never dropped", "not evidence that any risk factor was dropped", "No risk factors were removed", "None of
# NVIDIA's risk factors were dropped", "Zero of the 24 ... were removed", "found no risk factor ... that no longer appears".
# The "no" of "no longer" is itself a removal verb, so it is never the quantifier.
#
# NEGATION (:func:`_negated`). A negator cancels the verb up to eight words ahead of it (an aside between two commas, two
# dashes or parentheses does not count, nor does a filing named the way the prompt requires, "fiscal year ended January 26, 2025",
# nor a ", however," right after the negator; two words for a "without / rather than / instead of" that FRONTS its clause, one for
# "unlike", unless a comma closes it right after the verb; "failed / unable TO remove X" negates like "did not remove X"),
# unless the scope is cut: by a contrast or a presupposing word (``but``, ``because``, ``why``, ``what``, ``than``, ``now that``,
# ``due to``, a causal ``as it ...`` ...), by a colon or a semicolon, by a comma / dash that opens a new statement ("..., it removed
# X") or an aside it does not close ("Not surprisingly, Nvidia removed X"), or by an "and" ("plus", "&", "as well as") that starts a
# clause ("did not add new risks and also removed X"). A full stop ends it too, whatever follows it, unless it is an
# abbreviation's ("Micron Technology, Inc. was", "Jan. 26"). Words that only look like negators ("not only", "whether or not", "not a
# coincidence", "did not hesitate to", "wasted no time") assert. Shapes reach further than eight words, because the removal is then
# INSIDE what is negated: an inability verb or "unclear / unverified" ahead of ``whether`` ("the check cannot tell whether ...
# or was removed"), a negated evidence verb ("does not show / indicate / mean that ..."), a negated "... as removed", a denial ("it
# would be wrong to say that ..."), a bare ``whether`` / "if any" / "any claim that", and a removal verb coordinated with an earlier
# negated one ("was removed and is no longer disclosed"). The negation does NOT reach a removal the sentence takes for granted:
# a relative clause on a definite noun ("the risk factor it removed"), an attributive participle ("the removed risk factor"), the
# object of an act of telling ("no explanation for dropping X", "did not comment on the removal of X", "did not flag that IT
# removed X"), a noticed fact, and a negated verb that belongs to a relative clause on the subject ("The risk factor Nvidia never
# updated was removed"). NONE-QUANTIFIERS (:func:`_quantified`) reach the same way, from the start of their clause without limit,
# otherwise twelve words, or (rule d, not for a heading) as far as a relative pronoun ("found no risk factor from the fiscal year
# ended December 28, 2024 filing that no longer appears"); an exception ahead of one ("Apart from the X risk factor, no risk factor
# was removed") or after the verb ("... none was removed except the X one") makes the claim of that item, unless it excepts only
# the rewording itself ("Apart from rewording, ..."); "Nvidia removed NONE of its risk factors" is one too. A NAMED EMPTY LIST ("...
# no longer appear as a separate risk factor: none found", "the removed list is empty") survives, with no item after it. Known
# limits, pinned in tests/test_verify_removal_negation.py and tests/test_verify_removal_regressions.py (LIMIT_FP / LIMIT_FN): a
# negation of more than eight words that is none of these shapes, a negated complement with a coordinated verb ("not evidence that
# X reassessed and dropped Y"), a determiner-led conjunct ("does not mean the risk factor and that statement were removed"), a
# double negation, the hedge wording used to define itself, and world-fact prose that uses a removal verb.
_NEGATORS = frozenset({"not", "never", "nor", "neither", "without", "cannot"})
_WEAK_NEGATORS = frozenset({"unable", "impossible", "unclear", "fail", "fails", "failed", "failing", "unverified", "unconfirmed",
                            "unknown", "undetermined", "uncertain", "unsettled", "unresolved"})       # only ahead of "whether"
# "It would be WRONG to conclude that Nvidia dropped ...", "it is INCORRECT to say ...": a denial of the claim that follows
_DENIAL_WORDS = frozenset({"wrong", "incorrect", "inaccurate", "false", "untrue", "misleading", "mistaken", "unfounded", "unsupported",
                           "unwarranted", "unjustified", "premature", "mistake", "erroneous", "unreasonable"})
_CLAIM_NOUNS = frozenset("claim claims assertion assertions suggestion assumption conclusion inference statement".split())
_COMPLEMENT_VERBS = frozenset("say conclude claim state assume infer describe suggest read imply characterize assert argue "
                              "treat call".split())
_NEGATOR_PHRASES = (("rather", "than"), ("instead", "of"), ("as", "opposed", "to"), ("different", "from"), ("distinct", "from"),
                    ("in", "contrast", "to"), ("open", "question"))
_CONTRACTION_RE = re.compile(rf"n{_A}t$")                            # didn't, wasn’t, wasnʼt
_INABILITY_CONTRACTION_RE = re.compile(rf"(?:can|could)n{_A}t$")
# what an inability verb takes as its complement: "whether" / "if", or "that" / "which" right after a verb of knowing ("cannot tell
# which", "cannot conclude that"; not "cannot find the risk factor that was removed")
_OPENERS = frozenset({"whether", "if"})
_KNOWING_VERBS = frozenset("conclude concluded confirm confirmed say said tell determine determined verify verified show shown "
                           "establish established know decide settle prove demonstrate state assert".split())
_NEGATION_SCOPE_WORDS = 8
_PHRASAL_SCOPE_WORDS = 2
_COORDINATED_NOUN_WORDS = 5                                         # "did not show the older AND NEWER FILINGS AS removed"
_OBJECT_COMPLEMENT_WORDS = 30                                       # "did not identify <a long noun phrase> as removed"
_MAX_NEGATOR_TOKENS = 160                                           # a negator further back than this reaches no verb
_SCOPE_BREAKERS = frozenset({"but", "yet", "however", "although", "whereas", "while", "though", "because", "since", "instead",
                             "except", "unless", "so", "then", "after", "before", "once", "why", "how", "besides", "unsurprising",
                             "unsurprisingly", "when", "whenever", "until", "where", "what"})
_ADVERB_BREAKERS = frozenset({"yet", "since", "once", "so"})        # "has not YET / SINCE / ONCE / SO much ...": at k == 0 an adverb
_BREAKER_PHRASES = (("other", "than"), ("apart", "from"), ("aside", "from"), ("save", "for"), ("with", "the", "exception"),
                    ("now", "that"), ("given", "that"), ("due", "to"), ("owing", "to"), ("thanks", "to"), ("as", "a", "result"),
                    ("in", "which"), ("as", "well", "as"))
# Words that make the removal a fact the sentence takes for granted, whatever is negated before it: "no analyst NOTICED that ...".
# Not "show / say / state / confirm / identify" (those deny a finding: "does not show that X was removed").
_FACTIVE_VERBS = frozenset("notice noticed notices noticing realize realized realise realised aware".split())
# Acts of telling presuppose a removal when it is the OBJECT of the act ("did not comment on the REMOVAL of X", "the reason for
# DROPPING it") or what the COMPANY did ("did not flag that IT removed X", "no warning that it would drop X"), not when a finding
# about a risk factor follows: "Nvidia never announced that the risk factor had been removed" denies the announcement.
_PRESUPPOSING_VERBS = frozenset(
    "comment comments commented commenting explain explains explained explaining announce announced announces announcing flag flags "
    "flagged flagging highlight highlights highlighted highlighting tell tells told telling notify notified notifies warn warned "
    "acknowledge acknowledged acknowledges admit admitted admits justify justified justifies address addressed addresses addressing "
    "emphasize emphasized stress stressed publicize publicized elaborate elaborated clarify clarified".split())
_REMOVAL_NOMINALS = frozenset({"removal", "removals", "elimination", "deletion"})
# ... and the nouns of the same acts, when they take a preposition ("no EXPLANATION FOR dropping", not "no reason TO believe")
_PRESUPPOSING_NOUNS = frozenset("reason reasons explanation explanations announcement announcements warning warnings justification "
                                "rationale".split())
_NOUN_PREPOSITIONS = frozenset("for about behind of on why that from over regarding in as by".split())
# "shows that", "indicates that", "is evidence that": a negated evidence verb reaches its whole that-clause
_EVIDENCE_VERBS = frozenset("show shows showed shown indicate indicates indicated establish establishes established confirm confirms "
                            "confirmed support supports supported demonstrate demonstrates demonstrated prove proves proved suggest "
                            "suggests suggested reveal reveals revealed imply implies implied mean means meant say says said state "
                            "states stated report reports reported find finds found identify identifies identified verify verifies "
                            "verified evidence".split())
_EVIDENCE_REACH_WORDS = 30
_NEW_STATEMENT_LEADS = frozenset("it they he she we i there nvidia amd micron tsmc intel meta asml samsung".split())
_MATCH_CUT_WORDS = frozenset({"but", "yet", "however", "although", "whereas", "while", "because", "since", "so", "then", "after", "before",
                              "once", "why", "how", "when", "whenever", "until", "where", "what", "unless", "though"})
_CAUSAL_AS_RE = re.compile(r"\bas\s+(?:it|its|the|they|their|this|that|these|those|nvidia)\b", re.I)    # "... AS ITS wording was not found"
_NONE_WORDS = frozenset({"no", "none", "not", "nor", "never", "neither", "nothing"})
_AND_WORDS = frozenset({"and", "plus", "&"})
_HARD_MARKS = frozenset(":;!?")
_NEW_STATEMENT_RE = re.compile(r"[:;–—]|--|\s-\s")   # inside a match: a colon or a dash starts a statement of its own
_TOKEN_RE = re.compile(r"\w+(?:[.'’‘ʼ/-]\w+)*|--+|[—–]|(?<=\s)-(?=\s)|[,;:()!?&]")
_LIST_MARKER_RE = re.compile(r"^\s*(?:[-*•+]|\d+[.)])\s*")
# what an exception ahead of a none-quantifier excepts: a disclosure ("Apart from the indebtedness risk factor, ...") or the
# rewording itself ("Apart from rewording / the reworded items / wording differences, ...")
_EXCEPTED_NOUN_RE = re.compile(r"\b(?:risk\s+factors?|risks?|item\s+1a|disclosures?|statements?|paragraphs?|sentences?|ones?)\b", re.I)
_REWORDING_RE = re.compile(r"\b(?:reword\w*|wording|worded|changes?|changed|differences?|updates?|updated|revis\w+|edit\w*|modif\w+|"
                           r"new|added|additions?)\b", re.I)
# A clause about a disclosure that names none, but says what happened to "it": "Yes, it was removed [id]", "Nvidia has since removed
# it", "... both were dropped"; and a terse bullet or table cell: "- Indebtedness: removed [id]", "| Debt | no longer included |"
_REMOVAL_WORD = r"(?:removed|dropped|deleted|eliminated|omitted|discontinued)"
_ANAPHORIC_REMOVAL_RE = re.compile(
    rf"\b(?:it|they|both|these|those)\s+(?:was|were|is|are|has\s+been|have\s+been|had\s+been)\s+{_REMOVAL_WORD}\b|"
    rf"\b{_REMOVAL_WORD}\s+(?:it|them|both)\b", re.I)
_TERSE_REMOVAL_RE = re.compile(
    rf"(?:^|[:|—–]|\s-\s)\s*(?:was\s+|were\s+)?(?:{_REMOVAL_WORD}|no\s+longer\s+(?:included|disclosed|listed|present|appears?))"
    r"(?:\s*,\s*(?:not|never)\s+\w+)?\s*\|*\s*[.!]*\s*$", re.I)         # "Removed, not reworded [id]"
# a full stop ends a negation, whatever follows it (a closing quote, bracket or emphasis mark may come first)
_SENTENCE_BREAK_RE = re.compile(r"(?<=[a-z0-9)\]”\"’']{2}[.!?…])[)\]”\"’'*_`]*\s+")
# An abbreviation's full stop ("Micron Technology, Inc. was", "Jan. 26", "etc. as", "No. 12") is not a sentence end: masked before any
# split, and only where the next word does not start a sentence (a lower-case word or a digit), so "... Inc. Nvidia removed" splits.
_ABBREV_MASK = chr(0xE002)
_ABBREV_RE = re.compile(
    r"\b(?:inc|corp|co|ltd|llc|plc|etc|vs|approx|jan|feb|mar|apr|jun|jul|aug|sept?|oct|nov|dec|dr|mr|mrs|ms|st|fig)"
    r"\.(?=\s+(?-i:[a-z0-9]))|\bnos?\.(?=\s*\d)", re.I)
_LEGAL_COMMA_RE = re.compile(r",(?=\s+(?:inc|corp|co|ltd|llc|plc)\b)", re.I)     # "Micron Technology, Inc.": the comma is part of the name


def _mask_abbreviations(text: str) -> str:
    return _ABBREV_RE.sub(lambda m: m.group()[:-1] + _ABBREV_MASK, _LEGAL_COMMA_RE.sub("", text))


# Idioms whose negator asserts: "not only", "no doubt", "cannot be denied", "did not hesitate", "not the first time", "wasted no time"
_ASSERTIVE_RE = re.compile(
    r"\bnot\s+(?:only|just|least|(?:un)?surprising(?:ly)?|unexpected(?:ly)?)\b|\bwhether\s+or\s+not\b|"
    r"\bcan(?:not|['’]t)\s+(?:be\s+denied|deny|doubt|ignore|overlook|hide)\b|\bunsurprising(?:ly)?\b|"
    r"(?:\bnot|n['’]t)\s+(?:hesitat\w*|deny|dispute|contest|wait|forget|fail)\b|"
    r"\b(?:no|not\s+an?|without\s+an?)\s+(?:real\s+|serious\s+)?(?:doubt|question|secret|surprise|coincidence|accident)\b|"
    r"\b(?:should|must|can|could|will|would)(?:\s+not|n['’]t)\s+(?:be\s+)?(?:forgotten|overlook\w*|ignor\w*|miss\w*|lose\s+sight)\b|"
    r"\bnot\s+(?:hard|difficult|easy|the\s+first\s+time|un(?:true|likely|usual|common|reasonable|important|natural|fair|wise)|"
    r"fewer\s+than|less\s+than|a\s+few|a\s+little)\b|"
    r"\b(?:did|does|do|has|have|had)(?:\s+not|n['’]t)\s+take\s+long\b|\bno\s+(?:one|body)\s+(?:can\s+)?(?:deny|denies|dispute\w*|"
    r"doubt\w*|question\w*|contest\w*)\b|\bno\s+(?:denying|wonder|time|small|few|little|trivial|minor)\b|"
    r"\bthink(?:ing)?\s+twice\b|\bwasted\s+no\b", re.I)
# ("no matching text" is the answer prompt's own hedge for a removed item, "for which the text check found no matching text,
#  no longer appears ...": that "no" is the check's finding, not a "no risk factors were removed" quantifier.)
# A compound ("zero-COVID", "no-China-sales"), an abbreviation ("No. 12") and a bare numeral ("fell to 0") are not quantifiers: "0"
# is one only before "of" or a disclosure noun ("0 risk factors were removed", "0 of the 24 ...").
_NONE_LEADS = (rf"no(?![-–]\w)(?!\s*[.{_ABBREV_MASK}]\s*\d)(?!\s+(?:longer|match\w*|fewer|less|more|other|small|few|little)\b)|none|"
               r"zero(?![-–]\w)|nothing|neither|not\s+any|not\s+a\s+single|"
               r"0(?=\s+(?:of\b|(?:\w+\s+)?(?:risk|passage|paragraph|item|factor|disclosure|sentence|statement|change)))")
_NONE_QUANTIFIER_RE = re.compile(rf"\b(?:{_NONE_LEADS})\b", re.I)
_EXCEPTION_WORDS = r"apart\s+from|aside\s+from|other\s+than|except(?:\s+for)?|besides|save\s+for|with\s+the\s+exception"
_EXCEPTION_RE = re.compile(rf"\b(?:{_EXCEPTION_WORDS})\b", re.I)
_EXCEPTION_OR_BUT_RE = re.compile(rf"\b(?:{_EXCEPTION_WORDS}|but)\b", re.I)         # "removed nothing BUT the Taiwan risk factor"
_QUANTIFIER_WINDOW_WORDS = 12       # how far before the verb a "no ... were removed" object may start
_RELATIVE_GAP_WORDS = 15            # words allowed between "that / which" and the verb: "at any distance within the clause", within reason
_AUXILIARIES = frozenset("is are was were has have had been being also now later then".split())
_DEFINITE = frozenset({"the", "this", "these", "those", "its", "their"})
_INDEFINITE = frozenset({"any", "a", "an", "no", "none", "zero", "every", "each", "some", "either", "all", "many", "one", "which"})
_REMOVAL_PARTICIPLES = frozenset("removed dropped deleted eliminated omitted discontinued".split())
# "the risk factor (that) Nvidia / it removed": a subject right after a disclosure noun makes the verb a relative clause on it
_RELATIVE_SUBJECTS = frozenset("it they he she we you i nvidia amd micron tsmc intel meta asml samsung company management".split())
_ADVERBS = _ADVERB_BREAKERS | frozenset("also now later then ever still just only even really actually always".split())
_NP_STARTERS = frozenset("the a an its their this that these those one ones".split())      # what starts an excepted noun phrase
_MEMBERSHIP = frozenset("among of in on from within".split())
_AS_COMPARATIVES = frozenset("far long soon much many well often early late".split())      # "as far as", "as long as": no clause of its own
_NOT_BREAKING_THAN = frozenset("fewer less more greater rather better worse higher lower".split())
_SUBJECT_AUXILIARIES = frozenset("had has have would will could also now later then".split())
_RELATIVE_FOLLOWERS = _AUXILIARIES | _RELATIVE_SUBJECTS | frozenset({"the", "its", "their", "a", "an"})     # what follows a relative "that"
_DISCLOSURE_NOUNS = frozenset("risk risks factor factors passage passages sentence sentences paragraph paragraphs statement statements "
                              "item items disclosure disclosures".split())
_FINITE = frozenset("is are was were has have had be been being do does did can could will would may might shall should must".split())
_PASSIVE = frozenset("is are was were be been being".split())
_CLAUSE_STARTERS = frozenset("the a an its their this that these those it they he she we then also so not no now later one two three four "
                             "five some several many all each every any both most another other few".split())
# A word before "that" that makes it a complementizer ("shows that Nvidia removed", "no doubt that ..."), not a relative pronoun:
_COMPLEMENT_LEADS = frozenset(
    "shows show showed shown says said states stated notes noted means meant indicates indicated suggests suggested reveals "
    "revealed confirms confirmed implies implied doubt question secret surprise surprising unsurprising clear evident obvious "
    "apparent true fact evidence denied mean".split())
_COORDINATED_GAP_RE = re.compile(
    r"[\s,\"'“”]*(?:(?:and|or|nor)\s+)?(?:(?:is|are|was|were|has|have|had|been|being|also|now)\s+){0,2}", re.I)
# A word of the statement that a list is empty. It never crosses a contrast, a carve-out ("none EXCEPT the X", "none OTHER than",
# "none BESIDES") or a subordinate clause that starts a claim of its own ("... because its debt is now zero", "... since sales were
# 0", "... now that debt is zero"): "as" is one only when it is not "as a separate risk factor" / "as removed".
_TAIL_STOP = (r"(?:and|but|however|although|while|yet|though|because|since|except|besides|unless|where|when|whereas|so|given|due|"
              r"other|apart|aside|save|excluding|beyond|barring|as(?!\s+(?:a|an|the|separate|removed|dropped|being|having)\b)|"
              r"now(?=\s+that\b))")
_TAIL_WORD = rf"(?!{_TAIL_STOP}\b)[\w'’/-]+"
_TAIL_WORDS = 18
# "none", and "no risk factors" / "no items" for a label that says what it lists
_NONE_WORD = (r"(?:empty|none(?!\s+of\b)|nothing(?!\s+(?:else|more|further)\b)|zero|0|n/a|"
              r"no\s+(?:risk\s+factors?|items?|entries|entry|passages?|paragraphs?|matches?|results?)"
              r"(?:\s+(?:listed|found|identified|(?:were|was|are|is)\s+listed))?)")
_NONE_SUBJECT = (r"(?:(?:the|this)\s+)?(?:(?:automated|text)\s+)*(?:(?:check|comparison|list)\s+(?:found|lists?|shows?|identified|"
                 r"contains?|reports?)\s+|there\s+(?:are|is|were|was)\s+)?")
# After the verb: "... no longer appear as a separate risk factor: none found", "the removed risk factors list is empty",
# "Removed risk factors: the text check found none"
_NONE_TAIL_RE = re.compile(
    rf"(?:\s+{_TAIL_WORD}|\s*\)){{0,{_TAIL_WORDS}}}?(?:\s*[:—–,]\s*{_NONE_SUBJECT}|\s+-\s+{_NONE_SUBJECT}|\s+(?:is|are|was|were|shows?|"
    rf"lists?|reads?|has|have|contains?|found|reports?)\s+)(?:(?:all|both|now|currently)\s+)?{_NONE_WORD}\b"
    r"(?!\s+(?:others?|else|more|further)\b)", re.I)
_RANGE_AND_RE = re.compile(r"(?<=\d)\s+and\s+(?=[A-Za-z]*\s*\d)")      # "fiscal 2025 and fiscal 2026": a range, not a new clause
_SHORT_HEADING_WORDS = 6            # "**Dropped:**", "Removed risk factors:": a heading needs no disclosure noun of its own
_SHORT_LABEL_WORDS = 12             # a plain line this short that ends in a colon names what follows ("Removed risk factors:")
_MAX_REMOVAL_SENTENCE_CHARS = 200
_BULLET_RE = re.compile(r"^\s*(?:[-*•+]|\d+[.)])\s+\S")
_RULE_RE = re.compile(r"^\s*(?:[-=_*]\s*){3,}$")
# A statement that a list has nothing in it, in the model's own words: "None found.", "The text check found none", "This comparison
# shows none", "The text check did not identify any", "This list is empty for this comparison". It cites nothing, excepts nothing,
# and (:func:`_none_item`) is not itself a removal claim.
_NONE_MARKER_RE = re.compile(
    r"\b(?:none|nothing|zero|empty|n/a)\b(?!\s+(?:others?|else|more|further)\b)|\bno\s+(?!longer\b|match\w*\b)\w+|"
    r"\bnot\s+(?:\w+\s+){0,3}?any\b|(?<![\w.,%$-])0(?![\w.,%-])|\bnot\s+applicable\b", re.I)
_NONE_LEAD_RE = re.compile(r"\W*(?:none|nothing|zero|0|n/a|not\s+applicable|empty|no\b(?!\s+longer))", re.I)
_NONE_ANCHOR_RE = re.compile(r"\b(?:check|comparison|list|category|section|context|filings?|found|identified|listed|shows?|lists|"
                             r"contains?|reports?|available|entries|items?|there)\b", re.I)
# Text the check reads as a section name or a date, not as words of a claim:
_DATE_COMMA_RE = re.compile(r"\b([A-Z][a-z]{2,8}\.?\s+\d{1,2}),\s+(\d{4})\b")     # "January 26, 2025"
# ... the naming the answer prompt requires ("the fiscal year ended January 26, 2025 10-K", "the 10-K filed 2025-02-26") is ONE word
# of a negation's reach: it names a filing, it holds no claim
_FISCAL_PERIOD_RE = re.compile(r"(?:the\s+)?fiscal\s+years?\s+ended\s+[A-Za-z]{3,9}\.?\s+\d{1,2}\s+\d{4}|(?:the\s+)?fiscal\s+\d{4}\b", re.I)
_FILED_DATE_RE = re.compile(r"(?<=10-K|20-F)\s+filed\s+\d{4}-\d{2}-\d{2}\b", re.I)
# the context's "REMOVED / ADDED / REWORDED", and the same title written with commas ("Removed, added and reworded risk factors")
_SECTION_NAME_RE = re.compile(
    r"\bremoved\s*/\s*added(?:\s*/\s*reworded)?\b|\bremoved\s*,\s*(?:added|new)\s*,?\s*(?:and\s+|or\s+)?reworded\b", re.I)
_CAPS_REMOVED_RE = re.compile(r"\bREMOVED\b")                                     # answer.txt's "the REMOVED list"
_NOT_MATCHED_RE = re.compile(r"\bNot matched\b")                                  # a list label: its "Not" negates nothing
_RANGE_DASH_RE = re.compile(r"(?<=\d)–(?=[A-Za-z]*\d)")                           # "2024–2025", "FY2025–FY2026": a range
_WORD_RE = re.compile(r"\w+(?:['’‘ʼ-]\w+)*")


def _normalise(text: str) -> str:
    text = _DATE_COMMA_RE.sub(r"\1 \2", _plain_spaces(text))
    text = _CAPS_REMOVED_RE.sub("changed", _SECTION_NAME_RE.sub("changed", text))
    text = _FILED_DATE_RE.sub("", _FISCAL_PERIOD_RE.sub("fiscalperiod", text))
    return _RANGE_DASH_RE.sub("-", _NOT_MATCHED_RE.sub("Unmatched", text))


Tokens = list[tuple[str, int]]     # (lower-case word or punctuation mark, offset)


def _tokens(text: str) -> Tokens:
    return [(m.group().lower(), m.start()) for m in _TOKEN_RE.finditer(text)]


def _is_word(token: str) -> bool:
    return token[0].isalnum() or token[0] == "_"


def _words(scope: Tokens) -> list[str]:
    return [token for token, _ in scope if _is_word(token)]


def _object_complement(words: list[str]) -> bool:
    """The verb is what a negated verb takes as complement: "did not identify any risk factor as removed", "did not show a risk
    factor being dropped"."""
    return (words[-1:] in (["as"], ["being"]) or words[-2:] in (["as", "being"], ["as", "having"], ["having", "been"])
            or words[-3:] == ["as", "having", "been"])


def _is_dash(token: str) -> bool:
    return token in ("—", "–", "-") or token.startswith("--")


def _list_commas(tokens: list[str]) -> frozenset[int]:
    """The commas that separate the items of a list closed by "or" / "nor" ("no A, B, C, or D", "none of A, B or C"): they open no
    aside. A comma AFTER the list ("did not reword, trim, or shorten it, it removed it") does."""
    found: set[int] = set()
    for c, token in enumerate(tokens):
        if token not in ("or", "nor"):
            continue
        j = next((b for b in range(c - 1, max(-1, c - 5), -1) if tokens[b] == "," or not _is_word(tokens[b])), None)
        while j is not None and tokens[j] == ",":
            found.add(j)
            j = next((b for b in range(j - 1, max(-1, j - 5), -1) if tokens[b] == "," or not _is_word(tokens[b])), None)
    return frozenset(found)


_LIST_CONTINUES_RE = re.compile(r"\s*,?\s*(?:or|nor|and)\s+\w", re.I)


def _trim_open_list(scope: Tokens, suffix: str) -> Tokens:
    """The verb itself is an item of a list that goes on after it ("No risk factor was added, REMOVED or reworded"): the comma right
    before it separates items and opens no aside."""
    return scope[:-1] if scope and scope[-1][0] == "," and _LIST_CONTINUES_RE.match(suffix) else scope


def _causal_as(tokens: list[str], k: int) -> bool:
    """The "as" at ``tokens[k]`` opens a clause of its own ("... as it removed it", "as Nvidia deleted the risk factor"), not a
    complement ("as removed", "as being removed", "as no longer appearing", "as opposed to", "such as")."""
    rest = [t for t in tokens[k + 1:] if _is_word(t)]
    complement = rest[:1] and (
        rest[0] in _REMOVAL_PARTICIPLES or rest[0] in ("being", "having", "opposed", "no")          # "as removed or as no longer"
        or not {"being", "having"}.isdisjoint(rest[:4]))                                            # "as the company having removed X"
    comparative = ((tokens[k - 1:k] and tokens[k - 1] in _AS_COMPARATIVES)
                   or (rest[:1] and rest[0] in _AS_COMPARATIVES))                                        # "as FAR AS we can tell"
    adjective = (len(rest) == 1 and rest[0] not in _CLAUSE_STARTERS
                 and rest[0] not in _RELATIVE_SUBJECTS)                                         # "flagged as UNVERIFIABLE removals"
    return bool(rest) and not (complement or comparative or adjective) and tokens[k - 1:k] not in (["such"], ["same"])


def _hard_cut(scope: Tokens) -> bool:
    """True when the words between a negator (or none-quantifier) and the verb end its reach: a contrast or presupposing word, a
    colon / semicolon, a comma / dash that opens a new statement ("..., it removed X"), or one that opens an aside it does not
    close."""
    tokens = [token for token, _ in scope]
    commas = dashes = depth = 0
    listed = _list_commas(tokens)
    for k, token in enumerate(tokens):
        # "has not yet / since / once / so much shown ...": right after the negator these are adverbs; ", however," right after it an aside
        aside = k == 1 and tokens[0] == "," and tokens[2:3] == [","]
        if token in _HARD_MARKS or (token in _SCOPE_BREAKERS and not (token in _ADVERB_BREAKERS and k == 0) and not aside):
            return True
        if token == "than" and tokens[k - 1:k] and tokens[k - 1] not in _NOT_BREAKING_THAN:     # "nothing else changed THAN the removal"
            return True
        if (any(tuple(tokens[k:k + len(phrase)]) == phrase for phrase in _BREAKER_PHRASES)
                or (token == "as" and _causal_as(tokens, k))):
            return True
        if token == "," or _is_dash(token):
            if tokens[k + 1:k + 2] and tokens[k + 1] in _NEW_STATEMENT_LEADS and k not in listed:     # "None of Nvidia, AMD or Micron"
                return True
            if token == "," and k not in listed:
                commas += 1
            elif _is_dash(token):
                dashes += 1
        elif token == "(":
            depth += 1
        elif token == ")":
            depth -= 1
            if depth < 0:
                return True
    return bool(commas % 2 or dashes % 2 or depth)


def _verbal(word: str) -> bool:
    return word in _FINITE or word.endswith(("ed", "ing"))


def _coordinated_subject(before: list[str], rest: list[str], *, strict: bool = False) -> bool:
    """A subject whose noun phrase holds an "and": the subject of a none-quantifier ("None of the export and supply risk factors
    were removed") or of what a negated evidence verb takes ("does not show / mean the export and China risk factors were removed").
    No finite verb before the "and" (``strict``: no verb at all), and after it a noun phrase that closes on a passive verb ("... were
    / was found to"), never a determiner or a pronoun ("... and the export risk was removed" is a claim of its own)."""
    if (any(_verbal(w) if strict else w in _FINITE for w in before) or not rest or rest[0] in _CLAUSE_STARTERS
            or rest[0] in _FINITE or rest[0].endswith("ed") or rest[0].isdigit()):     # "and removed ...", "and marked it as ..." is a verb
        return False
    return any(word in _PASSIVE for word in rest[:4]) or rest[-1:] == ["as"]


def _and_cut(scope: Tokens, *, subject: bool = False, evidence: bool = False) -> bool:
    """True when the scope holds an "and" ("plus", "&", "as well as") that starts a claim of its own. Not "and no ..." (a second
    quantified noun), not "and" inside a noun phrase that ends in "as" ("did not show the older and newer filings as removed") and,
    for a none-quantified ``subject`` or what a negated ``evidence`` verb takes, not one inside its subject noun phrase
    (:func:`_coordinated_subject`)."""
    tokens = [token for token, _ in scope]
    for k, token in enumerate(tokens):
        if token not in _AND_WORDS:
            continue
        rest = _words(scope[k + 1:])
        if not rest:
            return True
        noun_phrase = (_object_complement(rest) and len(rest) <= _COORDINATED_NOUN_WORDS and rest[0] not in _CLAUSE_STARTERS
                       and not any(_verbal(w) for w in rest[:-1]))
        second_none = rest[0] in _NONE_WORDS and not _ASSERTIVE_RE.match(" ".join(rest))
        coordinated = (subject and _coordinated_subject(_words(scope[:k]), rest)) or (
            evidence and _coordinated_subject(_words(scope[:k]), rest, strict=True))
        if not (second_none or noun_phrase or coordinated):
            return True      # "and marked it as removed": a verb, so a claim
    return False


def _evidence_complement(words: list[str]) -> bool:
    """The negator takes an evidence verb ("does not SHOW / MEAN / INDICATE ...", "did not FIND that ..."): the removal is in its
    complement."""
    return any(word in _EVIDENCE_VERBS for word in words[:2])


def _counted_words(scope: Tokens) -> int:
    """Words of a scope that count towards its length: an aside between two commas, two dashes or parentheses does not ("did
    not, according to the comparison, drop ...", "not evidence — only a wording difference — that ...") and neither does a
    leading "yet" ("has not yet shown")."""
    count, comma, dash, depth = 0, False, False, 0
    tokens = [token for token, _ in scope]
    listed = _list_commas(tokens)
    for k, token in enumerate(tokens):
        if token == "," and k not in listed:
            comma = not comma
        elif _is_dash(token):
            dash = not dash
        elif token in "()":
            depth += 1 if token == "(" else -1
        elif _is_word(token) and not (comma or dash or depth or (k == 0 and token in _ADVERB_BREAKERS)):
            count += 1
    return count


def _relative_kind(words: list[str]) -> str | None:
    """``definite`` / ``indefinite`` when the words end in a relative clause on a noun of that kind: "the risk factor THAT was
    removed", "the risk factor it removed" (definite: the removal is a fact the sentence takes for granted, whatever is negated
    before it: "cannot find the risk factor that was removed"), "any risk factor that was removed" (indefinite: it asks whether
    there is one). A "that" after a verb of saying ("shows that Nvidia removed") is a complementizer, no relative."""
    n = len(words)
    for pos in range(n - 1, max(-1, n - 5), -1):
        if words[pos] in ("that", "which"):
            if pos == 0 or words[pos - 1] in _COMPLEMENT_LEADS or words[pos - 1] in _EVIDENCE_VERBS:
                return None
            following = words[pos + 1:pos + 2]
            if following and following[0] not in _RELATIVE_FOLLOWERS:       # "or THAT statement was": a demonstrative
                continue
            if following and following[0] not in _AUXILIARIES and words[pos - 1] not in _DISCLOSURE_NOUNS:
                continue                          # "conclude from the text alone THAT Nvidia deleted": a complementizer
            return _antecedent_kind(words[:pos])
    if n >= 2 and words[-1] in _RELATIVE_SUBJECTS and words[-2] in _DISCLOSURE_NOUNS:
        return _antecedent_kind(words[:-1])
    return None


def _antecedent_kind(words: list[str]) -> str | None:
    """The kind of the noun phrase the relative clause closes: "definite" (the ...), "indefinite" (any / no / a ...), and a noun
    phrase that names a SET a thing is or is not a member of ("is not AMONG the risk factors that were removed", "not ONE OF the
    ...") is indefinite: it does not say that any was."""
    for idx in range(len(words) - 1, max(-1, len(words) - 7), -1):
        word = words[idx]
        if word in _INDEFINITE or word in _DEFINITE:
            return "definite" if word in _DEFINITE and not (idx and words[idx - 1] in _MEMBERSHIP) else "indefinite"
    return None


def _presupposes(words: list[str], matched: str, comparison: bool = True) -> bool:
    """The words between a negator (or a none-quantifier) and the removal verb ``matched`` make the removal a fact the sentence
    takes for granted, so the negation does not cancel it: a relative clause on a definite noun ("the risk factor it removed"), an
    attributive participle ("has not reinstated the REMOVED risk factor"), a gerund after "for / about" ("the reason for DROPPING
    it") or an act of telling ("did not explain / flag / announce ...", "no explanation for ...") that is not itself asking
    whether it happened ("did not explain WHETHER it removed ..."). Only the relative clause holds outside a ``comparison``: in
    an answer about anything else "The filing does not explain the removal of the tariff" is world-fact prose."""
    if _object_complement(words):
        return False
    relative = _relative_kind(words)
    verb = matched.strip().lower()
    attributive = words[-1:] and words[-1] in _DEFINITE and verb in _REMOVAL_PARTICIPLES and not (
        len(words) > 1 and words[-2] in _MEMBERSHIP)                 # "reinstated the REMOVED risk factor", not "not among the removed"
    if relative == "definite" or (comparison and (attributive or (words[-1:] in (["for"], ["about"]) and verb.endswith("ing")))):
        return True
    if relative == "indefinite" or not comparison:
        return False
    # the removal is the act's object ("... the removal of X", "... for dropping X") or what the COMPANY did ("... that IT removed X"),
    # not a finding about a risk factor ("... that the risk factor had been removed")
    told_of = verb.endswith("ing") or verb in _REMOVAL_NOMINALS or _company_did_it(words)
    for k, word in enumerate(words):
        acts = told_of and (
            (word in _PRESUPPOSING_VERBS and "as" not in words[k + 1:k + 4])                          # "risk factors FLAGGED AS ..." is no act
            or (word in _PRESUPPOSING_NOUNS and not _NOUN_PREPOSITIONS.isdisjoint(words[k + 1:k + 4])))
        if (word in _FACTIVE_VERBS or acts) and _OPENERS.isdisjoint(words[k + 1:]):
            return True
    return False


def _company_did_it(words: list[str]) -> bool:
    """The words end in the subject of the removal verb, and it is the company or a pronoun: "... that IT / NVIDIA (had) removed"."""
    tail = words[:-1] if words[-1:] and words[-1] in _SUBJECT_AUXILIARIES else words
    return bool(tail) and tail[-1] in _RELATIVE_SUBJECTS


def _relative_negation(words: list[str], matched: str) -> bool:
    """The negated verb is the verb of a relative clause on the SUBJECT, and the removal is the main predicate: "The risk factor
    Nvidia never UPDATED was removed" (one verb, then the passive verb), "The risk factor Nvidia did not REWORD no longer appears"
    (one verb, then an intransitive removal). "Nvidia did not say the risk factor was removed" has a subject of its own between."""
    if not words or words[0] in _PASSIVE or words[0] in _EVIDENCE_VERBS or words[0] in _ADVERBS or words[0].endswith("ly"):
        return False                      # "has not YET / ALSO / EVER been removed": an adverb, no verb
    if len(words) == 2 and words[1] in _PASSIVE:
        return True
    return len(words) == 1 and matched.strip().lower().startswith("no longer")


def _reaches_its_clause(words: list[str]) -> bool:
    """The negator takes a whole that-clause: "does not SHOW THAT ...", "is not EVIDENCE THAT ...", "is not the case that ...", "is not
    true that ..." (a filing named in full ahead of the verb is longer than eight words)."""
    for k, word in enumerate(words[:4]):
        if word in _EVIDENCE_VERBS and words[k + 1:k + 2] == ["that"]:
            return True
    return (words[:3] == ["the", "case", "that"] or words[:2] == ["true", "that"]
            or (words[:1] in (["accurate"], ["correct"], ["right"], ["fair"]) and words[1:2] in (["to"], ["that"])))


def _scope_holds(scope: Tokens, kind: str, matched: str = "", closed: bool = False, comparison: bool = True) -> bool:
    """Whether a negator of ``kind`` (see :func:`_negation_at`) reaches the end of ``scope``, the words up to the removal verb
    ``matched``. ``closed``: a comma follows the verb within three words, so a fronted phrase ("Instead of being removed, ...") holds
    it."""
    words = _words(scope)
    opener = kind in ("inability", "weak", "denial") and any(
        word in (_COMPLEMENT_VERBS if kind == "denial" else _OPENERS) or (
            kind != "denial" and word in ("that", "which") and _KNOWING_VERBS.intersection(words[max(0, k - 6):k]))
        for k, word in enumerate(words[:5] if kind == "denial" else words))
    opener = opener or (kind == "denial" and words[:1] == ["that"])          # "it is false THAT any risk factor was removed"
    fails_to = kind == "weak" and words == ["to"]                    # "The company FAILED TO remove the risk factor"
    if (kind in ("weak", "denial") and not opener and not fails_to) or _hard_cut(scope) or _presupposes(words, matched, comparison):
        return False
    if fails_to:
        return True
    if kind in ("plain", "inability") and _relative_negation(words, matched):
        return False
    evidence = kind == "plain" and _evidence_complement(words)
    if opener or kind == "whether":       # "cannot tell whether <anything> was removed": the removal is inside the negated question
        return not _and_cut(scope)        # ... but "... whether it was reworded AND the risk factor was removed" claims
    if kind in ("phrasal", "unlike"):      # "Unlike Micron Nvidia dropped X": "unlike" takes one word ("Unlike a removed risk factor, ...")
        limit = _NEGATION_SCOPE_WORDS if closed else (_PHRASAL_SCOPE_WORDS if kind == "phrasal" else 1)
    else:
        limit = _EVIDENCE_REACH_WORDS if _reaches_its_clause(words) else _NEGATION_SCOPE_WORDS
    return (_counted_words(scope) <= (_OBJECT_COMPLEMENT_WORDS if _object_complement(words) else limit)
            and not _and_cut(scope, evidence=evidence))


def _negation_at(tokens: Tokens, i: int) -> tuple[int, str] | None:
    """(words the negator takes, its kind) or None. Kinds: ``inability`` (cannot, could not: reaches a whether / that clause of
    any length), ``weak`` (unable, unclear, unverified, failed: a negator only ahead of such a clause), ``denial`` (wrong,
    incorrect: "it would be wrong to say that ..."), ``phrasal`` (without, rather than, instead of: reaches two words when it
    FRONTS the sentence: "Without much fanfare Nvidia removed the risk factor" claims) and ``plain``."""
    word = tokens[i][0]
    if word in _WEAK_NEGATORS:
        return 1, "weak"
    if word in _DENIAL_WORDS:
        return 1, "denial"
    if word == "unlike":
        return 1, "unlike"
    if word == "without":
        return 1, "phrasal"
    if word in _NEGATORS:
        modal = word == "not" and i > 0 and tokens[i - 1][0] in ("could", "can")
        return 1, "inability" if word in ("cannot", "nor") or modal else "plain"
    if _CONTRACTION_RE.search(word):
        return 1, "inability" if _INABILITY_CONTRACTION_RE.search(word) else "plain"
    ahead = tuple(token for token, _ in tokens[i:i + 3])
    for phrase in _NEGATOR_PHRASES:
        if ahead[:len(phrase)] == phrase:
            return len(phrase), "weak" if phrase == ("open", "question") else "phrasal"
    if ahead[:2] == ("if", "any"):                                   # "Which risk factors, if any, were removed": a question
        return (3 if ahead[2:3] == (",",) else 2), "whether"
    if ahead[:1] == ("any",) and ahead[1:2] and ahead[1] in _CLAIM_NOUNS and ahead[2:3] == ("that",):
        return 3, "whether"                                          # "any claim that Nvidia dropped X would be unsupported"
    return None


def _within(spans: list[tuple[int, int]], position: int) -> bool:
    return any(start <= position < end for start, end in spans)


def _fronts_its_clause(tokens: Tokens, i: int) -> bool:
    """No word stands between ``tokens[i]`` and the start of its clause (the sentence, or the last comma / colon / dash)."""
    return not (i and _is_word(tokens[i - 1][0]))


def _closes_soon(suffix: str) -> bool:
    """A comma follows within three words: "Instead of being removed, ..." """
    return bool(re.match(r"(?:\s*[\w'’-]+){0,3}\s*,", suffix))


def _negated(tokens: Tokens, spans: list[tuple[int, int]], matched: str = "", suffix: str = "", comparison: bool = True) -> bool:
    """True when a negator among ``tokens`` (the words before a removal verb ``matched``) reaches the verb (see the block comment
    above). ``whether`` opens a negated question by itself, when nothing ahead of it negates."""
    closed, seen = _closes_soon(suffix), False
    for i in range(max(0, len(tokens) - _MAX_NEGATOR_TOKENS), len(tokens)):
        found = _negation_at(tokens, i)
        if found is None and tokens[i][0] == "whether":
            found = (1, "whether")
        if found is None or _within(spans, tokens[i][1]) or (found[1] == "whether" and seen):
            continue
        seen = seen or found[1] != "whether"
        kind = "plain" if found[1] in ("phrasal", "unlike") and not _fronts_its_clause(tokens, i) else found[1]
        if _scope_holds(_trim_open_list(tokens[i + found[0]:], suffix), kind, matched, closed, comparison):
            return True
    return False


def _relative_ahead(scope: Tokens) -> bool:
    """A relative pronoun ("that", "which", "whose") close to the verb: "... filing that no longer appears"."""
    words = _words(scope)
    for k, word in enumerate(words):
        if word in ("that", "which", "whose") and (words[k - 1] if k else "") not in _COMPLEMENT_LEADS:
            if len(words) - k - 1 <= _RELATIVE_GAP_WORDS + (word == "whose"):
                return True
    return False


def _carved_out(before: str) -> bool:
    """An exception ahead of a none-quantifier that excepts a DISCLOSURE ("Apart from the indebtedness risk factor, no risk factor
    was removed") says that one was: the quantifier does not cancel the verb. An exception of the rewording itself ("Apart from
    rewording, no risk factor was removed", "Other than the reworded items, nothing was dropped") excepts no disclosure."""
    return any(_EXCEPTED_NOUN_RE.search(before[e.end():]) and not _REWORDING_RE.search(before[e.end():])
               for e in _EXCEPTION_RE.finditer(before))


def _quantified(prefix: str, spans: list[tuple[int, int]], *, relative: bool, matched: str = "", suffix: str = "",
                comparison: bool = True) -> bool:
    """True when a none-quantifier ("no", "none of", "zero", "0") governs the verb: it is the subject of its clause, or stands
    within twelve words of the verb, or is followed by a relative pronoun ("found no risk factor from ... that no longer appears"),
    with nothing in between that ends its reach (:func:`_hard_cut`, :func:`_and_cut`, :func:`_presupposes`)."""
    for m in _NONE_QUANTIFIER_RE.finditer(prefix):
        before = prefix[:m.start()]
        if _within(spans, m.start()) or _carved_out(before):
            continue
        scope = _trim_open_list(_tokens(prefix[m.end():]), suffix)
        words = _words(scope)
        clause_start = _LIST_MARKER_RE.sub("", re.split(r"[,;:—–(\"“]", before)[-1])
        subject = not any(_is_word(t) for t, _ in _tokens(clause_start))        # nothing between it and the start of its clause
        if _hard_cut(scope) or _and_cut(scope, subject=subject) or _presupposes(["no"] + words, matched, comparison):
            continue
        attribute = bool(re.search(r"\bwith\s*$", before, re.I))      # "risk factors WITH NO counterpart ... that were removed"
        window = len(words) + 5 * words.count("fiscalperiod")               # a period, "the fiscal year ended January 26, 2025", is 6 words here
        if window <= _QUANTIFIER_WINDOW_WORDS or (not attribute and subject):
            return True
        if relative and not attribute and _relative_ahead(scope):
            return True
    return False


_AND_CLAUSE_RE = re.compile(
    r"\b(?:and|plus)(?:\s*,[^,.;]{0,40},)?\s+(?:also\s+|then\s+)?(?:the|a|an|its|their|this|that|these|those|it|they|so|now|then)\b", re.I)


def _cuts_negation(matched: str, *, quantifier: bool = False) -> bool:
    """True when the text a removal pattern matched itself holds a contrast word or a colon / dash: a match that opens with its
    subject noun ("risks: 7 of 7 passages had wording not found in the newer filing") reaches over words that end a negation
    standing before that noun ("... without indicating removal of the underlying risks: ..."), and over the "and" that starts a
    clause of its own ("no new risk factors AND the sentence's wording was not found"). A none-``quantifier`` before a match that
    lists its nouns ("No risk factors, passages or paragraphs had wording not found") is cut only by a clause: the words of a list
    ("risk factors and paragraphs") do not end it."""
    words = {w.lower() for w in _WORD_RE.findall(matched)}
    cut = (bool(_NEW_STATEMENT_RE.search(matched)) or not _MATCH_CUT_WORDS.isdisjoint(words) or bool(_CAUSAL_AS_RE.search(matched)))
    if quantifier:
        return cut or bool(_AND_CLAUSE_RE.search(matched))
    return cut or "," in matched or not _AND_WORDS.isdisjoint(words)


def _none_tail(suffix: str, after: str = "") -> bool:
    """The text after a removal verb says its list is empty ("... no longer appear as a separate risk factor: none found", "the
    removed list is empty"), with no exception ("... none except the X", "empty apart from the X") and no item (``after``: the
    clauses that follow, "none found, but the X risk factor [id]") after it."""
    text = _RANGE_AND_RE.sub(" to ", re.sub(r"[*_`]+", "", suffix))
    found = _NONE_TAIL_RE.match(text)
    return (bool(found) and not _EXCEPTION_RE.search(text[found.end():]) and not re.search(r"\bbut\b", text[found.end():], re.I)
            and not _EXCEPTION_RE.search(after) and not CITE_RE.search(after))


_CLAIM_THAT_RE = re.compile(r"\b(?:claims?|assertions?|suggestions?|assumptions?|notions?|ideas?|conclusions?|inferences?|beliefs?)"
                            r"\s+that\b[^.;:]*$", re.I)
_DENIED_AFTER_RE = re.compile(
    r"^[^.;]{0,60}?\b(?:(?:is|are|was|were|would\s+be|remains?)\s+(?:not\s+(?:supported|true|correct|accurate|justified|established|"
    r"verified|shown)|unsupported|false|incorrect|wrong|untrue|unfounded|inaccurate|unproven|unverified|unwarranted|misleading))\b", re.I)


def _survives(prefix: str, matched: str = "", suffix: str = "", *, relative: bool = True, after: str = "",
              comparison: bool = True) -> bool:
    """True when the words before a removal verb (``prefix``; ``matched`` is the text of the match, ``suffix`` what follows it)
    negate it, make its subject "no / none / zero of ...", or say that the list it names is empty. "The claim that X was removed
    is not supported" denies the claim after its verb."""
    prefix = _SENTENCE_BREAK_RE.split(_mask_abbreviations(prefix))[-1]
    spans = [m.span() for m in _ASSERTIVE_RE.finditer(prefix)]
    if ((_negated(_tokens(prefix), spans, matched, suffix, comparison) and not _cuts_negation(matched))
            or (_CLAIM_THAT_RE.search(prefix) and _DENIED_AFTER_RE.match(suffix))
            or (_quantified(prefix, spans, relative=relative, matched=matched, suffix=suffix, comparison=comparison)
                and not _cuts_negation(matched, quantifier=True))
            or _NONE_OBJECT_RE.match(suffix)):
        return not _carved_after(suffix)       # ... unless an exception after the verb names one that was ("... except the Russia one")
    return _none_tail(suffix, after)


# "Nvidia removed NONE of its risk factors", "dropped NO risk factors", "deleted NOTHING": the object is the none-quantifier
_NONE_OBJECT_RE = re.compile(
    r"\s*(?:none|nothing|zero|0|no(?!\s+(?:fewer|less|more|other|longer|match))|not\s+(?:a\s+single|any|one))\b", re.I)


def _carved_after(suffix: str) -> bool:
    """An exception after the verb that excepts a DISCLOSURE ("was removed except the Russia one", "removed nothing but the Taiwan
    risk factor") says that one was: a noun phrase, with no verb after it ("... but kept the export risk factor" is a second clause,
    and "... except that one was renamed" a third)."""
    sentence = re.split(r"[.;]", suffix, maxsplit=1)[0]
    for e in _EXCEPTION_OR_BUT_RE.finditer(sentence):
        rest = sentence[e.end():]
        words = [w.lower() for w in _WORD_RE.findall(rest)]
        if (words and words[0] in _NP_STARTERS and _EXCEPTED_NOUN_RE.search(rest) and not _REWORDING_RE.search(rest)
                and not any(w in _FINITE or w.endswith("ed") for w in words)):
            return True
    return False


def _claims_removal(words: str, *, heading: bool = False, disclosure: bool = False, before: str = "", after: str = "",
                    comparison: bool = False) -> bool:
    """True when ``words`` (a clause without its bracketed labels) asserts that a disclosure went away. A ``heading``
    (a line with bullets under it) may be short and name no disclosure itself: "**Dropped:**"; so may a ``disclosure`` clause
    that continues a sentence about one ("..., but has been removed", "; it was deleted"). ``before`` / ``after``: the clauses of
    the same sentence around this one: a negation ahead can still reach the verb ("does not show that Nvidia, which reworded X,
    removed Y"), and what follows a "none found" can still name an item ("none found, but the X risk factor [id]").
    ``comparison``: the context holds a comparison between two filings, so "Yes, it was removed" and "| X | Removed |" speak of a
    disclosure although they name none (in an answer about anything else "it was discontinued" is world-fact prose)."""
    words = _normalise(words)
    names_none = comparison and (_ANAPHORIC_REMOVAL_RE.search(words) or _TERSE_REMOVAL_RE.search(words))
    if not (disclosure or _DISCLOSURE_RE.search(words) or names_none or (heading and len(words.split()) <= _SHORT_HEADING_WORDS)):
        return False
    prior = _normalise(before)
    survived_to = None
    for m in _REMOVAL_RE.finditer(words):
        coordinated = survived_to is not None and _COORDINATED_GAP_RE.fullmatch(words[survived_to:m.start()])
        if not (coordinated or _survives(prior + words[:m.start()], m.group(0), words[m.end():], relative=not heading, after=after,
                                         comparison=comparison)):
            return True
        survived_to = m.end()      # "removed, deleted, or stopped disclosing" / "removed and is no longer" share a negation
    return False


def _shortened(text: str) -> str:
    shown = " ".join(text.split())
    return shown if len(shown) <= _MAX_REMOVAL_SENTENCE_CHARS else shown[:_MAX_REMOVAL_SENTENCE_CHARS - 3].rstrip() + "..."


def _indent(line: str) -> int:
    return len(line) - len(line.lstrip(" \t"))


_BOLD_LABEL_ONLY_RE = re.compile(r"^\s*[-*+•]\s+(?:\*\*|__)[^*_]+(?:\*\*|__):?\s*$")     # "- **Reworded:**"


def _bullets_under(lines: list[str], i: int, *, siblings: bool = False) -> tuple[list[str], int]:
    """The bullet lines directly under ``lines[i]`` (blank lines skipped) and the index after them. Under a bullet that
    is only its more indented bullets ("- **Removed:**" then "  - item"); under any other line, every bullet. ``siblings``: the
    bullet at ``lines[i]`` is nothing but a list label, so the bullets that follow at its own level are its items, up to the next
    label ("- **No longer appears as a separate risk factor:**" then "- Indebtedness risk [id]")."""
    base = _indent(lines[i]) if _BULLET_RE.match(lines[i]) and not siblings else -1
    bullets: list[str] = []
    j = i + 1
    while j < len(lines):
        line = lines[j]
        if not line.strip():
            j += 1
        elif _BULLET_RE.match(line) and _indent(line) > base and not (
                siblings and (_BOLD_LABEL_ONLY_RE.match(line) or _echo(line) is not None)):
            bullets.append(line)
            j += 1
        else:
            break
    return (bullets, j) if bullets else ([], i + 1)


# The labels the context prints above its lists (context_layout.py: the reader matches on the same constants), as a regex. The
# answer prompt quotes them and the model copies them: as a heading ("### No longer appears as a separate risk factor" then "The
# text check found none"), as a bold, italic, bullet, numbered or table line, or quoted inside a sentence. A label names a list, it
# claims nothing; the items UNDER a removed-list label are judged by their ids (:func:`_list_items`). ``<units>`` is "risk
# factor(s)" / "paragraph(s)" or both, and the "of surviving <units>" middle of the passages label is optional.
def _words_re(text: str) -> str:
    return r"\s+".join(re.escape(word) for word in text.split())


_LABEL_UNITS = r"(?:risk\s+factors?|paragraphs?)(?:\s+(?:or|and)\s+(?:risk\s+factors?|paragraphs?))?"
_PASSAGES_HEAD, _, _PASSAGES_MIDDLE = PASSAGES_PREFIX.strip().partition(" ")         # "Passages" | "of surviving"
_LABEL = (rf"(?P<label>(?P<removed>{_words_re(REMOVED_ITEMS_PREFIX)}\s+{_LABEL_UNITS}"
          rf"|{_words_re(_PASSAGES_HEAD)}\s+(?:{_words_re(_PASSAGES_MIDDLE)}\s+{_LABEL_UNITS}\s+)?"
          rf"{_words_re(PASSAGES_REMOVED_PHRASE)})"
          rf"|(?P<unsettled>{_words_re(UNSETTLED_ITEMS_PREFIX.rstrip(' ('))}))")
_NONE_STATEMENT = (rf"{_NONE_SUBJECT}(?:none|nothing|zero|0|n/a|empty|no\s+(?:risk\s+factors?|items?|entries|passages?|paragraphs?|"
                   rf"matches?|results?))\b(?:\s+{_TAIL_WORD}){{0,8}}")
# what may follow a label on its line: "none found" in any wording, the context's own "- showing 2 of 21 risk factors (note):"; a
# parenthesis only when it is none / the context's own note ("the text check found no matching text ...", "a differently worded
# version ... may exist"), never a NAME: "(the indebtedness risk factor)" puts an item under the label
_PAREN_BODY = r"(?:(?!(?:except|besides|other\s+than|apart|aside|but)\b)[^()\[\]])*"
_ECHO_TAIL = (rf"(?:[\s\-–—:]*(?:(?P<none>{_NONE_STATEMENT})|showing\s+\d+\s+of\s+\d+(?:\s+{_TAIL_WORD}){{0,4}}"
              rf"|\((?:(?P<none2>none|nothing|empty)\b{_PAREN_BODY}|(?:showing|the\s+text\s+check|a\s+differently\s+worded|"
              rf"parts\s+of)\b{_PAREN_BODY})\)))*[\s:.]*")
_ECHO_RE = re.compile(rf"^{_LABEL}{_ECHO_TAIL}$", re.I)
_TAIL_VERB_RE = re.compile(r"\b(?:dropped|removed|deleted|eliminated|discontinued|stopped)\b", re.I)
_QUOTE = "[\"“”'‘’`]"
_QUOTED_TAIL = rf"(?:[\s\-–—:]*(?:none(?:\s+found)?|showing\s+\d+\s+of\s+\d+(?:\s+{_TAIL_WORD}){{0,4}}))?[\s,.:;!]*"
# The label in quotation marks, punctuation of the sentence allowed before the closing mark (`"Passages ... newer filing,"`)
_QUOTED_LABEL_RE = re.compile(rf"(?<!\w){_QUOTE}\s*{_LABEL}{_QUOTED_TAIL}{_QUOTE}", re.I)
_LABEL_PLACEHOLDER = "“label”"
_LIST_MENTION = "LISTLABEL"     # a quoted REMOVED-list label: naming an item under it is a removal claim for that item


def _undecorate(line: str) -> str:
    """The words of a line without its markdown: heading marks, bullet / number, emphasis, table pipes."""
    text = line.strip()
    if text.startswith("|"):
        text = text.strip("| ").replace("|", ": ")
    return re.sub(r"[*_`]+", "", re.sub(r"^(?:#{1,6}\s+|[-*•+]\s+|\d+[.)]\s+)+", "", text)).strip()


def _echo(line: str) -> tuple[str, bool] | None:
    """(``removed`` / ``unsettled``, states none found) when the whole line is a list label of the context (with what the
    context prints after it), else None. Only the label as the context capitalises it counts, unless the line is a heading or a
    bold line: the lower-case wording the prompt teaches ('the risk factor "no longer appears as a separate risk factor"') is a
    claim about one risk factor."""
    text = _undecorate(line)
    found = _ECHO_RE.match(text)
    if not found or (found["removed"] and _TAIL_VERB_RE.search(text[found.end("label"):])):
        return None
    if not (found["label"][:1].isupper() or line.strip().startswith(("#", "**", "__"))):
        return None
    return ("removed" if found["removed"] else "unsettled"), bool(found["none"] or found["none2"])


def _none_item(text: str) -> bool:
    """A list item that says there is none ("None found.", "The text check found none", "This comparison shows none", "The
    list is empty"): no ids, no exception ("None except the X"), and not itself a removal claim ("No, Nvidia removed it")."""
    plain = _undecorate(text)
    return (not CITE_RE.search(text) and not _EXCEPTION_RE.search(plain) and bool(_NONE_MARKER_RE.search(plain))
            and bool(_NONE_LEAD_RE.match(plain) or _NONE_ANCHOR_RE.search(plain))
            and not _claims_removal(_BRACKETED_RE.sub(" ", plain), comparison=True))


_TABLE_SEPARATOR_RE = re.compile(r"^\s*\|?\s*:?-{2,}:?\s*(?:\|\s*:?-{2,}:?\s*)*\|?\s*$")      # the "|---|---|" line under a table header


def _starts_section(line: str) -> bool:
    return line.lstrip().startswith("#") or _echo(line) is not None


def _list_items(lines: list[str], i: int, *, siblings: bool = True) -> tuple[list[str], int]:
    """The items under the list label at ``lines[i]``: the paragraph right below it (past blank lines), then the bullets after
    that (or directly under the label), and the index after them. ``siblings``: under a bullet that is only a label, the bullets
    at its own level are its items (:func:`_bullets_under`)."""
    j = i + 1
    while j < len(lines) and (not lines[j].strip() or _RULE_RE.match(lines[j])):      # a blank line, a "---" under a setext heading
        j += 1
    body: list[str] = []
    while j < len(lines) and lines[j].strip() and not _BULLET_RE.match(lines[j]) and not _starts_section(lines[j]):
        if not _TABLE_SEPARATOR_RE.match(lines[j]):
            body.append(lines[j])
        j += 1
    bullets, after = _bullets_under(lines, j - 1 if body else i, siblings=siblings and not body and bool(_BULLET_RE.match(lines[i])))
    return (body + bullets, after if bullets else (j if body else i + 1))


_LIST_NOUN_RE = re.compile(r"\s*(?:list|heading|section|category|group|line|bullet|table|column)\b", re.I)
_INTRODUCES_LABEL_RE = re.compile(r"\b(?:under|in|see|per|within|below|above|from)\s*$", re.I)


def _quoted_label(found: re.Match) -> str:
    """A quoted list label as the context capitalises it is a mention of the list. So is one in lower case that is said to be a
    list ("the "no longer appears ..." list is empty") or that a preposition introduces ("Under "no longer appears ..."")."""
    written_in_full = found["label"][:1].isupper() or _LIST_NOUN_RE.match(found.string, found.end()) or _INTRODUCES_LABEL_RE.search(
        found.string[:found.start()])
    if not written_in_full:
        return found.group(0)
    return _LIST_MENTION if found["removed"] else _LABEL_PLACEHOLDER


# A sentence end (followed by a capital, digit or bullet: "U.S. revenue" does not split), a semicolon, a line break: hard. A comma
# that opens a new statement ("..., and Nvidia plans ...") is soft: the ids of the clauses that follow a claim, up to the next
# claim, belong to it ("no longer appears as a separate risk factor, and parts of its content may be covered [id]": the answer
# prompt asks for the statement and its ids in one SENTENCE).
_HARD_SPLIT_RE = re.compile(r"(?<=[.!?…])[*_`\"”’)\]]*\s+(?=[A-Z0-9\-*(\"“])|\s*;\s*|\n+")
_SOFT_SPLIT_RE = re.compile(r",\s+(?=(?:and|but|while|whereas|which|although|though|yet|however|so)\b)")
# a clause that starts with a connective or a pronoun and goes straight to a removal verb: "it was deleted", "which was removed"
_CONTINUATION_RE = re.compile(
    r"^\W*(?:it|they|this|that|these|those|which|and|but|so|yet|while|whereas|although|though|nor)\s+"
    r"(?:(?:has|have|had|is|are|was|were|been|being|also|now|later|then|since|subsequently|not|never)\s+){0,3}"
    r"(?:drop|remov|delet|eliminat|discontinu|omit|stopp|ceas|no\s+longer)", re.I)
# ... with a subject of its own, when the context is a comparison: "which Nvidia removed", "which the company later dropped"
_WHICH_CLAUSE_RE = re.compile(r"^\W*which\s+(?:[\w,'’-]+\s+){1,6}?(?:(?:has|had|later|then|subsequently|also)\s+)*"
                              r"(?:drop|remov|delet|eliminat|discontinu|omit|stopp|ceas)", re.I)
# a clause that only restates the one before it: ", which means the text check found ...", ", so it no longer appears ...", ", and
# the text check found no matching text ...": it shares the ids of the sentence (a NEW subject, ", and the export risk factor was
# removed", does not)
_RESTATES_RE = re.compile(r"^\W*(?:(?:and|but|so|yet|while|whereas|although|though|however)\s+)?"
                          r"(?:which|it|they|this|that|these|those|(?:the\s+)?(?:text\s+)?(?:check|comparison)\b)", re.I)
# "The <list label> list DOES NOT INCLUDE the Taiwan risk factor": the item is NOT on it
_LIST_NEGATED_RE = re.compile(
    r"\s*(?:list\s+)?(?:(?:does|do|did|would|will|can)(?:\s+not|n['’]t)|has\s+no|lists\s+no|shows\s+no|contains\s+no)\s+\w+", re.I)
# "... is ABSENT FROM the <list label> list": the item is NOT on it
_ABSENT_FROM_RE = re.compile(r"\b(?:absent|missing|excluded|lacking|not\s+(?:listed|included|shown|present|found))\s+"
                             r"(?:from|in|on|under)\s+(?:the\s+)?$", re.I)
_QUOTED_SPAN_RE = re.compile(r"\"[^\"\n]{8,}?\"|“[^”\n]{8,}?”")
_GAP, _SEMI = "\ue000", "\ue001"      # a quoted filing sentence ("property, plant, and equipment; ...") is not split


def _mask_quotes(text: str) -> str:
    return _QUOTED_SPAN_RE.sub(lambda m: re.sub(r"(?<=[,.!?;])\s", _GAP, m.group()).replace(";", _SEMI), text)


def _claim_kind(clause: str, about_a_disclosure: bool, before: str = "", after: str = "", comparison: bool = False) -> str | None:
    """``verb`` when the clause says a disclosure went away, ``list`` when it names an item under a quoted removed-list label
    with no removal verb of its own (it cites an id, or names a disclosure: "The export risk is listed under "..."", not "See the
    "..." list"), else None. ``about_a_disclosure``: the line names one, so a clause that only continues it ("but has been
    removed", "it was deleted") speaks of it too. ``before`` / ``after``: the clauses of the sentence around this one."""
    words = _BRACKETED_RE.sub(" ", clause)           # "[Dropped Risk Lineages]" is a label, not a claim
    prior = _BRACKETED_RE.sub(" ", before)
    continues = _CONTINUATION_RE.match(words) or (comparison and _WHICH_CLAUSE_RE.match(words))
    if _claims_removal(words, disclosure=about_a_disclosure and bool(continues), before=prior, after=after, comparison=comparison):
        return "verb"
    if _LIST_MENTION in words and not _REMOVAL_RE.search(words):
        head, _, tail = words.partition(_LIST_MENTION)
        if _ABSENT_FROM_RE.search(head) or _LIST_NEGATED_RE.match(tail) or _survives(prior + head, "", tail):
            return None
        if CITE_RE.search(clause) or _DISCLOSURE_RE.search(head + " " + tail) or (about_a_disclosure and _RESTATES_RE.match(words)):
            return "list"
    return None


# "unverifiable for removal", "not verified as removal": the noun "removal" there says nothing was verified (the "Not matched" list's own
# wording, which the answer prompt asks for), not that a disclosure was removed.
_UNVERIFIED_REMOVAL_RE = re.compile(r"\b(?:unverifi\w+|not\s+verifi\w+)\s+(?:for|as)\s+removals?\b", re.I)
# "beyond the removal question", "the question asks about removal": the noun names what the QUESTION is about, it says nothing was removed.
_REMOVAL_QUESTION_RE = re.compile(r"\bremovals?\s+(?:question|part|aspect)\b|\b(?:asks?|asking)\s+(?:specifically\s+)?about\s+removals?\b", re.I)


def _clause_claims(line: str, supported: set[str], comparison: bool = False) -> list[str]:
    """Removal claims of one line, judged clause by clause: each must cite ids, and only ids of the removed lists. A claim
    with none of its own takes the ids of the comma-clauses that follow it (up to the next claim that is not a restatement of
    it: ", which means the text check found no matching text [id]"). A quoted list label is not a claim by itself (it names a list:
    "see the "..." list"); an item put under one is. A clause that says a listed item "is on the list", with nothing to cite,
    takes the ids of the clause before it ("X [id], which is on the "..." list")."""
    claims = []
    line = _REMOVAL_QUESTION_RE.sub("question", _UNVERIFIED_REMOVAL_RE.sub("unverified", line))
    about_a_disclosure = bool(_DISCLOSURE_RE.search(_BRACKETED_RE.sub(" ", line)))
    masked =_mask_abbreviations(_mask_quotes(_QUOTED_LABEL_RE.sub(_quoted_label, line)))
    earlier_ids: list[str] = []
    for part in _HARD_SPLIT_RE.split(masked):
        clauses = [c for c in (x.replace(_GAP, " ").replace(_SEMI, ";").replace(_ABBREV_MASK, ".").strip()
                               for x in _SOFT_SPLIT_RE.split(part)) if c]
        kinds = [_claim_kind(c, about_a_disclosure, ", ".join(clauses[:k]) + ", " if k else "", ", ".join(clauses[k + 1:]), comparison)
                 for k, c in enumerate(clauses)]
        for k, clause in enumerate(clauses):
            if kinds[k] is None:
                continue
            ids = CITE_RE.findall(clause)
            if not ids and kinds[k] == "list" and _RESTATES_RE.match(clause):
                ids = CITE_RE.findall(", ".join(clauses[:k])) or list(earlier_ids)
            if not ids:
                for other, kind in zip(clauses[k + 1:], kinds[k + 1:]):
                    if kind is not None and not _RESTATES_RE.match(other):
                        break
                    ids += CITE_RE.findall(other)
            if not (ids and set(ids) <= supported):
                claims.append(_shortened(clause))
        earlier_ids = CITE_RE.findall(part)
    return claims


_BOLD_LINE_RE = re.compile(r"^(?:[-*•+]\s+)?(?:\*\*|__).+(?:\*\*|__):?$")


def _is_heading_like(line: str, next_line: str = "") -> bool:
    """A markdown heading, a bold line or a table header row (the one over the "|---|---|" line): a line that names what follows, not
    a sentence. A data row of a table is no heading of the rows after it."""
    text = line.strip()
    return (text.startswith("#") or bool(_BOLD_LINE_RE.match(text)) or (text.endswith(":") and len(text.split()) <= _SHORT_LABEL_WORDS)
            or (text.startswith("|") and text.endswith("|") and bool(_TABLE_SEPARATOR_RE.match(next_line))))


def _heading_claims(head: str, items: list[str], supported: set[str]) -> list[str]:
    """A line that says something was removed, with items under it: the ids of its items (and its own) must all be
    ids of the removed lists. ``Removed: - None found.`` states no removal."""
    if all(_none_item(item) for item in items):
        return []
    ids = set(CITE_RE.findall(head)).union(*(CITE_RE.findall(item) for item in items))
    return [] if ids and ids <= supported else [_shortened(head)]


def unsupported_removal_claims(text: str, context: str) -> tuple[str, ...]:
    """Sentences of ``text`` that claim a disclosure was removed but cite something other than the ids under the temporal
    block's removed lists (or cite nothing at all), shortened for display.

    Judged per clause (a sentence, a semicolon part, a bullet). A line that makes the claim and has bullets directly
    under it ("**Removed risk factors** (at least 21; 8 listed):" then one bullet per item) is judged with its bullets:
    the ids of the bullets count as its citations. So is a copy of the context's removed-list label with items under it (bullets,
    a table, a line of prose): the items' ids must be removed ids. Negated or none-quantified statements ("No risk factors
    were removed") claim survival, not removal, and a label with nothing under it (or "none found") names a list."""
    supported = removal_supported_ids(context)
    comparison = TEMPORAL_HEADER in context
    lines = text.split("\n")
    claims: list[str] = []
    i = 0
    while i < len(lines):
        line = lines[i]
        echo = _echo(line)
        if echo is not None:
            items, after = _list_items(lines, i, siblings=not echo[1]) if echo[0] == "removed" else ([], i + 1)
            found = _heading_claims(line, items, supported) if items else []
            claims += found
            i = after if found else i + 1       # a heading that passes leaves its items to be judged like any other line
            continue
        bullets, after = _bullets_under(lines, i)
        # a quoted list label names a list: "The "No longer appears ..." list shows none found:" then a bullet is no heading of removals
        heading = _claims_removal(_BRACKETED_RE.sub(" ", _QUOTED_LABEL_RE.sub(_quoted_label, line)), heading=True, comparison=comparison)
        if bullets and heading:
            claims += _heading_claims(line, bullets, supported)
            i = after
            continue
        # a paraphrased heading with prose or a table under it ("### Removed risk factors"); a bold line that cites is a claim
        items, after = (_list_items(lines, i) if heading and not CITE_RE.search(line)
                        and _is_heading_like(line, lines[i + 1] if i + 1 < len(lines) else "") else ([], i + 1))
        if items:
            found = _heading_claims(line, items, supported)
            claims += found
            i = after if found else i + 1
            continue
        claims += _clause_claims(line, supported, comparison)
        i += 1
    return tuple(claims)
