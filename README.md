# BCH Network Check

A simple Python script to quickly verify whether the Bitcoin Cash (BCH) network appears to be functioning normally by querying 10 public Electrum servers.

## What it shows

- Block height
- Latest block hash
- Bits
- Difficulty
- Age of the latest block
- Delta relative to the consensus height

## Status rule

The script returns:

- `OK` if more than half of the responding sources agree on both height and hash, and the median age of the consensus block does not exceed 45 minutes
- `WARN` otherwise

## Usage

```bash
python bch_network_check.py
```

## Requirements

- Python 3
- Internet connection

## Notes

The script queries public Electrum servers and does not require a local BCH node.
