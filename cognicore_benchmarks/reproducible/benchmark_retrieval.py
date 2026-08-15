import time
import os
import random
import logging
from cognicore.memory.sqlite_backend import SQLiteMemoryBackend
from cognicore.memory.base import MemoryEntry, MemoryScope

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("benchmark")

def run_benchmark():
    # Setup
    db_path = "benchmark_test.db"
    if os.path.exists(db_path):
        os.remove(db_path)
        
    backend = SQLiteMemoryBackend(db_path)
    
    logger.info("Populating database with 10,000 synthetic memories...")
    start_time = time.time()
    
    for i in range(10000):
        entry = MemoryEntry(
            text=f"This is synthetic memory entry number {i}. It contains some random data and facts.",
            category="synthetic",
            memory_type="semantic"
        )
        backend.store(entry)
        
    logger.info(f"Populated in {time.time() - start_time:.2f} seconds.")
    
    # Benchmark FTS5 / BM25
    queries = [
        "synthetic memory",
        "random data",
        "entry number 5000",
        "facts and synthetic data"
    ]
    
    logger.info("Benchmarking search (BM25 Fallback)...")
    search_times = []
    
    for q in queries:
        t0 = time.time()
        results = backend.search(query=q, top_k=5)
        t1 = time.time()
        search_times.append(t1 - t0)
        logger.info(f"Query '{q}' returned {len(results)} results in {t1 - t0:.4f}s")
        
    avg_time = sum(search_times) / len(search_times)
    logger.info(f"Average BM25 search time across 10k records: {avg_time:.4f}s")
    
    # Cleanup
    backend.clear()
    if os.path.exists(db_path):
        os.remove(db_path)

if __name__ == "__main__":
    run_benchmark()
