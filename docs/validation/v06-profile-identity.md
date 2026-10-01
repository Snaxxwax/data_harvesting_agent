# Profile identity integration validation

The pinned `socid-extractor==0.1.1` release was exercised on two fixed, offline HTML
snippets from supported schemes. The MyAnimeList snippet yielded `mal_uid=1` and
`mal_username=Xinil`; the Weebly snippet yielded `uid=125320777` and
`weebly_site_id=183235046254098859`. All four expected identifiers were recovered
with literal evidence and character locators. This is fixture coverage, not a claim about
the current live versions of those sites or other schemes.

An HTTP fixture run captured both pages, then API replay with an explicit MyAnimeList
target and source rule matched exactly one profile and no Weebly profile. The original
job's observations and captures were unchanged; replay made zero requests. CLI replay
with `--investigation` produced the same target field. An unsupported value without
literal source evidence was omitted with a warning, and a parser error retained the
base HTML title extraction. See `tests/test_socid_adapter.py` for the fixtures and checks.

The published 0.1.1 package has a smaller scheme set than the upstream repository's
current `master` branch. Broader identifier recall and false-positive rates remain
unmeasured until representative captured profiles are assembled.
