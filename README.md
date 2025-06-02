# Semantic Scholar Citation Crawler

## Overview

The Semantic Scholar Citation Crawler is a Python script that performs a breadth-first traversal of paper citations, starting from one or more seed paper IDs. It uses:

- **Semantic Scholar Graph API** to fetch metadata and references for each paper.
- **PostgreSQL** to store:
  - A `processed_papers` table (to mark which paper IDs have been seen).
  - A `citations` table (to store directed edges: `citing_id → cited_id`).
- **Redis with a Bloom filter** (`redisbloom` module) to deduplicate paper IDs before querying or writing to SQL.
- **Tenacity** for retry logic on network requests (to handle transient failures or rate limiting).
- **Threading & signal handlers** for graceful shutdown when receiving SIGINT/SIGTERM.
- **Argument parsing** via `argparse` to support `--fresh` and `--resume` modes, plus an optional list of seed IDs.

Key features:

1. **Deduplication**:  
   - A Redis Bloom filter quickly filters out most “already seen” paper IDs.  
   - A PostgreSQL `processed_papers` table finalizes the check so that false positives from the Bloom filter get cleared.

2. **Deadlock‐safe inserts**:  
   - Both citation inserts (`citations` table) and processed markers (`processed_papers` table) use `INSERT ... ON CONFLICT DO NOTHING` inside a retry loop to survive occasional deadlocks.

3. **Rate limiting**:  
   - Semantic Scholar enforces 1 request/second across all endpoints.  
   - After every successful API call, we sleep 1 second.

4. **Graceful shutdown**:  
   - A global `shutdown_event` is set on SIGINT or SIGTERM, so the main loop can break out cleanly.

5. **Metrics logging**:  
   - At each batch, we log how many papers were fetched (`papers fetched = len(to_fetch)`) and how many new references were discovered (`new references discovered = len(ref_rows)`).

6. **Field‐of‐Study Filtering (Optional)**:  
   - The code is currently configured to only follow citations where **both** the parent and the child paper have at least one field in the `DESIRED_FIELDS` set (e.g., only crawl “Computer Science ↔ Mathematics”).  
   - If you want to crawl **all** citations (no filtering), remove or ignore the related `DESIRED_FIELDS` checks.  

---

## Table of Contents

1. [Prerequisites](#prerequisites)  
2. [Installation & Setup](#installation--setup)  
3. [Configuration (Hardcoded)](#configuration-hardcoded)  
4. [Database Schema](#database-schema)  
5. [Redis & Bloom Filter Setup](#redis--bloom-filter-setup)  
6. [How It Works: Architecture](#how-it-works-architecture)  
7. [Function Descriptions](#function-descriptions)  
   - [`send_request`](#send_request)  
   - [`fetch_references_paginated`](#fetch_references_paginated)  
   - [`filter_new_ids`](#filter_new_ids)  
   - [`safe_insert_citations`](#safe_insert_citations)  
   - [`mark_processed`](#mark_processed)  
   - [`chunked`](#chunked)  
   - [`init_db`](#init_db)  
   - [`main`](#main)  
8. [Running the Crawler](#running-the-crawler)  
9. [Logging & Metrics](#logging--metrics)  
10. [Performance & Troubleshooting](#performance--troubleshooting)  
11. [License](#license)  

---

## Prerequisites

1. **Python 3.7+**  
2. **Required Python packages** (install via `pip`):  
   ```bash
   pip install redis requests psycopg2-binary tenacity
