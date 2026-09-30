"""Gold-set tooling (docs/GOLD_LABELING_GUIDE.md, docs/NARRATIVE_SCHEMA.md section 10).

- ``select``: pick ~100 gold titles from the pilot candidates.
- ``template``: write a Google-Sheets-friendly CSV (plus dropdown lists and a README tab).
- ``importer``: turn the filled sheet into schema-valid ``gold_label`` records.
- ``pairs``: the "should / should NOT match" similarity pairs (interview risk 1).
"""
