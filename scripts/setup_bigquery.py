"""Create the demo dataset for the BI Copilot.

Uploads in chunks because BigQuery's sandbox mode silently truncates
large `load_table_from_dataframe` calls. Chunks of 5,000 rows work reliably.

Idempotent — safe to re-run.

Usage:
    python scripts/setup_bigquery.py --project my-gcp-project --rows 100000
"""

from __future__ import annotations

import argparse
import time
from datetime import date, datetime, timedelta

import numpy as np
import pandas as pd
from google.cloud import bigquery

DATASET_DESC = "Synthetic e-commerce dataset for the BI Copilot demo."

CATEGORIES = ["Electronics", "Apparel", "Home", "Beauty", "Sports", "Books", "Toys"]
COUNTRIES = ["FR", "DE", "ES", "IT", "NL", "BE", "PT", "GB"]
CHANNELS = ["web", "mobile_app", "store", "marketplace"]
STATUSES = ["delivered"] * 22 + ["returned"] * 6 + ["cancelled"] * 4

DATE_START = date(2022, 1, 1)
DATE_RANGE_DAYS = 1095

CHUNK_SIZE = 5000  # confirmed to work in sandbox; larger silently truncates


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser()
    p.add_argument("--project", required=True)
    p.add_argument("--dataset", default="bi_copilot")
    p.add_argument("--location", default="EU")
    p.add_argument("--rows", type=int, default=10_000_000)
    p.add_argument("--customers", type=int, default=200_000)
    p.add_argument("--products", type=int, default=5_000)
    p.add_argument("--seed", type=int, default=42)
    p.add_argument("--partition", action="store_true")
    return p.parse_args()


def ensure_dataset(client: bigquery.Client, dataset_id: str, location: str) -> None:
    ref = bigquery.Dataset(dataset_id)
    ref.location = location
    ref.description = DATASET_DESC
    client.create_dataset(ref, exists_ok=True)
    print(f"  ✓ dataset {dataset_id} ready")


def gen_customers(n: int, rng: np.random.Generator) -> pd.DataFrame:
    return pd.DataFrame({
        "customer_id": [f"cust-{i:08x}" for i in rng.integers(0, 2**31, size=n)],
        "customer_code": [f"cust_{i:07d}" for i in range(1, n + 1)],
        "country_code": rng.choice(COUNTRIES, size=n),
        "signup_date": [
            date(2020, 1, 1) + timedelta(days=int(d))
            for d in rng.integers(0, 1825, size=n)
        ],
        "is_b2b": rng.random(n) < 0.12,
        "lifetime_value_score": np.round(rng.random(n) * 5, 1),
    })


def gen_products(n: int, rng: np.random.Generator) -> pd.DataFrame:
    return pd.DataFrame({
        "product_id": [f"SKU-{i:06d}" for i in range(1, n + 1)],
        "product_name": [f"Product {i}" for i in range(1, n + 1)],
        "category": rng.choice(CATEGORIES, size=n),
        "unit_price_eur": np.round(5 + rng.random(n) * 495, 2),
        "is_discontinued": rng.random(n) < 0.15,
    })


def gen_orders(
    n: int,
    customers: pd.DataFrame,
    products: pd.DataFrame,
    rng: np.random.Generator,
) -> pd.DataFrame:
    cust_idx = rng.integers(0, len(customers), size=n)
    prod_idx = rng.integers(0, len(products), size=n)
    quantity = rng.integers(1, 5, size=n).astype(np.int64)

    unit_price = products["unit_price_eur"].to_numpy()[prod_idx]
    gross = np.round(unit_price * quantity, 2)
    net = np.round(gross * (0.85 + rng.random(n) * 0.15), 2)

    day_offsets = rng.integers(0, DATE_RANGE_DAYS, size=n)
    sec_offsets = rng.integers(0, 86400, size=n)
    order_dates = [DATE_START + timedelta(days=int(d)) for d in day_offsets]
    order_ts = [
        datetime.combine(DATE_START + timedelta(days=int(d)), datetime.min.time())
        + timedelta(seconds=int(s))
        for d, s in zip(day_offsets, sec_offsets)
    ]

    return pd.DataFrame({
        "order_id": [f"ord-{i:012x}" for i in rng.integers(0, 2**47, size=n)],
        "order_date": order_dates,
        "order_ts": order_ts,
        "customer_id": customers["customer_id"].to_numpy()[cust_idx],
        "country_code": customers["country_code"].to_numpy()[cust_idx],
        "product_id": products["product_id"].to_numpy()[prod_idx],
        "category": products["category"].to_numpy()[prod_idx],
        "channel": rng.choice(CHANNELS, size=n),
        "quantity": quantity,
        "gross_revenue_eur": gross,
        "net_revenue_eur": net,
        "order_status": rng.choice(STATUSES, size=n),
    })


def upload_chunked(
    client: bigquery.Client,
    df: pd.DataFrame,
    table_id: str,
    *,
    description: str,
    partition_field: str | None = None,
    cluster_fields: list[str] | None = None,
    location: str = "EU",
    chunk_size: int = CHUNK_SIZE,
) -> int:
    """Upload `df` in chunks. First chunk truncates, subsequent ones append."""
    if len(df) <= chunk_size:
        return _single_upload(
            client, df, table_id,
            description=description,
            partition_field=partition_field,
            cluster_fields=cluster_fields,
            location=location,
        )

    n_chunks = (len(df) + chunk_size - 1) // chunk_size
    print(f"    uploading {len(df):,} rows in {n_chunks} chunks of {chunk_size:,}")

    for i in range(n_chunks):
        chunk = df.iloc[i * chunk_size : (i + 1) * chunk_size]
        is_first = i == 0

        config = bigquery.LoadJobConfig(
            write_disposition=(
                bigquery.WriteDisposition.WRITE_TRUNCATE
                if is_first
                else bigquery.WriteDisposition.WRITE_APPEND
            ),
        )
        # Schema metadata only attaches on the first (truncating) write.
        if is_first:
            config.destination_table_description = description
            if partition_field:
                config.time_partitioning = bigquery.TimePartitioning(
                    type_=bigquery.TimePartitioningType.DAY,
                    field=partition_field,
                )
            if cluster_fields:
                config.clustering_fields = cluster_fields

        job = client.load_table_from_dataframe(
            chunk, table_id, job_config=config, location=location
        )
        job.result()
        if (i + 1) % 5 == 0 or i == n_chunks - 1:
            print(f"    chunk {i + 1}/{n_chunks} done ({(i + 1) * chunk_size:,} rows uploaded)")

    table = client.get_table(table_id)
    return table.num_rows


def _single_upload(
    client: bigquery.Client,
    df: pd.DataFrame,
    table_id: str,
    *,
    description: str,
    partition_field: str | None,
    cluster_fields: list[str] | None,
    location: str,
) -> int:
    config = bigquery.LoadJobConfig(
        write_disposition=bigquery.WriteDisposition.WRITE_TRUNCATE,
        destination_table_description=description,
    )
    if partition_field:
        config.time_partitioning = bigquery.TimePartitioning(
            type_=bigquery.TimePartitioningType.DAY,
            field=partition_field,
        )
    if cluster_fields:
        config.clustering_fields = cluster_fields

    job = client.load_table_from_dataframe(df, table_id, job_config=config, location=location)
    job.result()
    return client.get_table(table_id).num_rows


def main() -> None:
    args = parse_args()
    rng = np.random.default_rng(args.seed)

    client = bigquery.Client(project=args.project)
    dataset_id = f"{args.project}.{args.dataset}"

    print(f"\nBootstrapping {dataset_id}\n")
    ensure_dataset(client, dataset_id, args.location)

    # ── dim_customers ────────────────────────────────────────────────
    t0 = time.perf_counter()
    customers = gen_customers(args.customers, rng)
    actual = upload_chunked(
        client, customers, f"{dataset_id}.dim_customers",
        description="Customer dimension table",
        location=args.location,
    )
    print(f"  ✓ dim_customers ({actual:,} rows · {time.perf_counter() - t0:.1f}s)")
    if actual == 0:
        raise RuntimeError("dim_customers upload returned 0 rows")

    # ── dim_products ─────────────────────────────────────────────────
    t0 = time.perf_counter()
    products = gen_products(args.products, rng)
    actual = upload_chunked(
        client, products, f"{dataset_id}.dim_products",
        description="Product catalog",
        location=args.location,
    )
    print(f"  ✓ dim_products ({actual:,} rows · {time.perf_counter() - t0:.1f}s)")
    if actual == 0:
        raise RuntimeError("dim_products upload returned 0 rows")

    # ── fct_orders ───────────────────────────────────────────────────
    t0 = time.perf_counter()
    orders = gen_orders(args.rows, customers, products, rng)

    partition_field = "order_date" if args.partition else None
    cluster_fields = ["country_code", "category"] if args.partition else None

    actual = upload_chunked(
        client, orders, f"{dataset_id}.fct_orders",
        description="Transactional order fact table",
        partition_field=partition_field,
        cluster_fields=cluster_fields,
        location=args.location,
    )
    elapsed = time.perf_counter() - t0
    layout = (
        "partitioned by order_date · clustered by country_code, category"
        if args.partition else "no partitioning (sandbox-friendly)"
    )
    print(f"  ✓ fct_orders ({actual:,} rows · {elapsed:.1f}s · {layout})")
    if actual == 0:
        raise RuntimeError("fct_orders upload returned 0 rows after chunked write")

    print("\nDone.")


if __name__ == "__main__":
    main()