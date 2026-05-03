Fix package

1. Replace index.html in GitHub with this file.
   - Today button is warm green.
   - Today uses local date, not UTC.
   - The page auto-rolls to today's date unless you manually choose an archive date.

2. Replace schedule_loader.py in GitHub with this file.
   - It now prefers human-readable TIPLOC descriptions over numeric NALCO codes.

3. Run the GitHub Action: Acton Bridge Schedule Loader.
   - This reloads Acton Bridge schedule data with proper names.

4. In Supabase SQL Editor, run fix_numeric_origin_destination.sql.
   - This clears old numeric-only movement origin/destination values.
   - Then run the Schedule Loader again, or wait for the next enrichment run.
