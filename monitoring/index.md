# Self-directed monitoring sweep

Connected components over the device-sharing graph, bounded to 2-10 cards, ranked by money moved since 2016-11-02. No benchmark card appears in any of these rings: nobody asked for this work.

| case | ring | $ in window | verdict | p | pattern | SAR | final actions |
|---|---:|---:|---|---:|---|---|---|
| [MON-001](MON-001.json) | 4 | 17,539.13 | uncertain | 0.39 | account_takeover | False | MONITOR_CARD, DECLINE_TRANSACTION, ESCALATE_TO_ANALYST, CREATE_CASE, MONITOR_CONNECTED_CARDS |
| [MON-002](MON-002.json) | 2 | 17,501.41 | uncertain | 0.63 | account_takeover | False | MONITOR_CARD, DECLINE_TRANSACTION, ESCALATE_TO_ANALYST, CREATE_CASE, MONITOR_CONNECTED_CARDS |
| [MON-003](MON-003.json) | 2 | 8,597.00 | fraud | 0.7 | account_takeover | True | MONITOR_CARD, DECLINE_TRANSACTION, ESCALATE_TO_ANALYST, CREATE_CASE, MONITOR_CONNECTED_CARDS, FILE_REPORT |
| [MON-004](MON-004.json) | 3 | 7,102.69 | uncertain | 0.38 | card_not_present_fraud | False | MONITOR_CARD, DECLINE_TRANSACTION, ESCALATE_TO_ANALYST, CREATE_CASE, MONITOR_CONNECTED_CARDS |
| [MON-005](MON-005.json) | 2 | 6,885.45 | uncertain | 0.63 | account_takeover | False | MONITOR_CARD, DECLINE_TRANSACTION, ESCALATE_TO_ANALYST, CREATE_CASE, MONITOR_CONNECTED_CARDS |
