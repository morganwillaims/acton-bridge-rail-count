# Acton Bridge Rail Count collector

Files:
- collector.py
- requirements.txt
- .github/workflows/collector.yml

Required GitHub repository secrets:
- NETWORK_RAIL_USERNAME
- NETWORK_RAIL_PASSWORD
- SUPABASE_URL
- SUPABASE_SERVICE_ROLE_KEY

The collector listens to Network Rail TRAIN_MVT_ALL_TOC briefly and writes Acton Bridge STANOX 37001 movements into Supabase.
