# Tool surface review: CustomerService

Every decision below is a proposal unless it is marked applied. A planner decision
carries a confidence below 1.0; a decision a reviewer already recorded carries 1.0.

## Summary

| Item | Count |
|---|---|
| Source operations | 1 |
| Planned artifacts | 1 |
| Decisions | 5 |
| Awaiting a reviewer | 0 |
| Blocking ambiguities | 1 |

## Planned surface

| Name | Kind | Risk | Group | Suitability | Review |
|---|---|---|---|---|---|
| `get_customer` | tool | read | CustomerPortType | 0.1667 | approved |

## Surface kind changes

| Target | Change | Origin | Confidence | Status | Rationale |
|---|---|---|---|---|---|
| `GetCustomer` | `unknown` to `read` | human | 1.0 | applied | Side effect recorded by a reviewer in the overlay. WSDL carries no signal to infer one from, so this is a judgement rather than a derivation, and it is the only thing that can release a SOAP operation through the gate. |

## Renames

| Target | Change | Origin | Confidence | Status | Rationale |
|---|---|---|---|---|---|
| `GetCustomer` | `GetCustomer` to `get_customer` | planner | 0.4 | applied | No summary was available, so the name falls back to the source identifier 'GetCustomer'. A reviewer should replace it with a task-oriented name. |

## Grouping

| Target | Change | Origin | Confidence | Status | Rationale |
|---|---|---|---|---|---|
| `GetCustomer` | `-` to `CustomerPortType` | planner | 0.65 | applied | Grouped by the SOAP port type 'CustomerPortType', which plays the role a path prefix plays for HTTP. |

## Approvals

| Target | Change | Origin | Confidence | Status | Rationale |
|---|---|---|---|---|---|
| `GetCustomer` |  | human | 1.0 | applied | Approved by a reviewer in the overlay, releasing it through the emission gate. |

## What a reviewer must decide

Every proposal has been recorded in the overlay.

Decisions already accepted by a reviewer: 2.
