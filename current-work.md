Follow-up validation identified regressions in the dynamic pool discovery branch. This change set removes the incomplete routing features and restores stable behaviour before pushing upstream.

## Completed
- Reverted the accidental override of MarketDataProvider.best_price so quoting logic works again.
- Dropped the unfinished dynamic routing helpers and CLI entry-points that referenced them.
- Removed the DexPool table and the unfinished pool_watcher module.
- Updated the repository docs to reflect the current, static TRADE_PATH-based routing model.

## Outstanding
- If automatic pool discovery is still a roadmap item, it needs a fresh implementation with tests and CLI smoke coverage before reintroducing it.
