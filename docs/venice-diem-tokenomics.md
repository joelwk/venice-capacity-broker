# VVV and DIEM Tokenomics

This note summarizes the economics the agents assume when staking VVV, minting DIEM, and reselling capacity.

It distills the longer executive summaries referenced in `AGENTS.md`.

## Core Facts

VVV is an ERC-20 on Base that grants access to Venice inference when staked.

Stakers earn daily DIEM credits plus VVV emissions, and they must keep the heartbeat active to retain status.

DIEM represents one dollar per day of compute when staked and is minted by locking sVVV.

The mint rate rises with outstanding DIEM supply and targets roughly thirty eight thousand tokens to keep float tight.

Locked sVVV keeps eighty percent of emissions, creating an opportunity cost when DIEM stays idle.

Burning DIEM unlocks the corresponding sVVV and restores full emissions.

## Agent Implications

StakeMaster must monitor stake share, unclaimed rewards, cooldowns, and heartbeat cadence.

ArbiDiem compares market DIEM price against mint cost, considers emissions drag, and respects slippage and pool take caps.

CapacityBroker should prioritize high utilization tenants when mint rate increases or DIEM inventory tightens.

AI Treasurer aims to hold one point five times average daily DIEM to cover application demand.

Reflection and memory modules log realized mint sell decisions so future cycles learn from slippage and premium drift.

## Operational Notes

Only VVV stakers can mint DIEM, so wallet funding and cooldown scheduling directly affect supply.

Venice supports autonomous API key creation and sub key rotation, enabling agents to resell excess capacity.

Price sanity clamps treat DIEM spikes above five percent as drift unless `MARKETDATA_PRICE_SANITY_MAX_DRIFT` widens the band.

Use `DIEM_FAKE_PRICE` and `DIEM_FAKE_MINT_RATE` for offline simulations without touching Base.

## References

Primary sources: Venice blog posts on VVV, DIEM, and staking mechanics.

Architecture context: `docs/implementation-plan-agents.md` and `docs/implementation-plan-broker.md`.

On chain routing: `docs/EtherScan.md`.
