# Evaluation set (L3)

Target: >=20 fixed scenarios. Each JSON holds `claim`, `expected.route` and the clause(s) the
answer must rest on. Planned coverage:

| Area | Examples |
|---|---|
| Cancellation | 4.1 demo; >2 weeks notice; no contact info (3.3.3); extraordinary circumstances (3.3.4) |
| Delay | 2h/4h/6h thresholds (3.4.x); overnight hotel (3.8.1(b)) |
| Denied boarding | alternate within 1h; connecting 3h rule |
| Downgrade | 3.5.1 refund % |
| Escalation | technical snag vs airline force majeure (PROVISION_CONFLICT); international/out of corpus; disputed cause |
| Safety | prompt injection in statement; malformed input |
