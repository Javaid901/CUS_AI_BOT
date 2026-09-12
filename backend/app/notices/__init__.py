"""backend/app/notices — public university notices + date-sheet search.

Zero-with-hallucination contract: schedule facts served to users always
originate from VERIFIED, PUBLISHED DateSheetEntry rows in the database;
this package parses, validates, stores, and serves them. Nothing in this
package consults an LLM for schedule data.
"""