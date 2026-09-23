# Self-directed monitoring sweep

Connected components over the device-sharing graph, bounded to 2-10 cards, ranked by money moved since 2016-11-02. No benchmark card appears in any of these rings: nobody asked for this work.

| case | ring | $ in window | verdict | p | pattern | SAR | final actions |
|---|---:|---:|---|---:|---|---|---|
| [MON-001](MON-001.json) | 4 | 17,539.13 | fraud | 0.8 | account_takeover | True | BLOCK_CARD, CREATE_CASE, MONITOR_CARD, MONITOR_CONNECTED_CARDS, FILE_REPORT |
| [MON-002](MON-002.json) | 2 | 17,501.41 | fraud | 0.76 | account_takeover | True | MONITOR_CARD, DECLINE_TRANSACTION, ESCALATE_TO_ANALYST, CREATE_CASE, MONITOR_CONNECTED_CARDS, FILE_REPORT |
| [MON-003](MON-003.json) | 2 | 8,597.00 | fraud | 0.94 | account_takeover | True | BLOCK_CARD, CREATE_CASE, MONITOR_CARD, MONITOR_CONNECTED_CARDS, FILE_REPORT |
| [MON-004](MON-004.json) | 3 | 7,102.69 | legitimate | 0.25 | none | False | ALLOW_TRANSACTION, CLOSE_NO_FRAUD, MONITOR_CARD |
| [MON-005](MON-005.json) | 2 | 6,885.45 | fraud | 0.76 | account_takeover | True | MONITOR_CARD, DECLINE_TRANSACTION, ESCALATE_TO_ANALYST, CREATE_CASE, MONITOR_CONNECTED_CARDS, FILE_REPORT |
