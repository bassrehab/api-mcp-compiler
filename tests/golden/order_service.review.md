# Tool surface review: Synthetic Order and Refund Service

Every decision below is a proposal unless it is marked applied. A planner decision
carries a confidence below 1.0; a decision a reviewer already recorded carries 1.0.

## Summary

| Item | Count |
|---|---|
| Source operations | 4 |
| Planned artifacts | 5 |
| Decisions | 21 |
| Awaiting a reviewer | 2 |
| Blocking ambiguities | 0 |

## Planned surface

| Name | Kind | Risk | Group | Suitability | Review |
|---|---|---|---|---|---|
| `approve_refund_and_release_payment` | tool | write | refunds | 0.6667 | proposed |
| `create_refund_request` | tool | write | refunds | 0.8333 | approved |
| `list_customer_orders` | resource | read | customers | 0.6667 | proposed |
| `look_up_customer` | resource | read | customers | 0.8333 | proposed |
| `refund_order` | composite | write | - | n/a | approved |

## Composite workflows

| Target | Change | Origin | Confidence | Status | Rationale |
|---|---|---|---|---|---|
| `createRefund_then_approveRefund` | `createRefund`, `approveRefund` | planner | 0.45 | applied | 'approveRefund' cannot be called without a refunds identifier, and 'createRefund' is what yields one. The specification states the dependency; exposing the two separately makes the agent rediscover it. |
| `createRefund_then_approveRefund` | `createRefund`, `approveRefund` | planner | 0.5 | applied | 'createRefund' prepares a resource that 'approveRefund' then approves. Exposing them separately lets an agent take the irreversible step without the step that makes it meaningful. |
| `createRefund_then_approveRefund` | `createRefund`, `approveRefund` | human | 1.0 | applied | Composite accepted by a reviewer in the overlay. |

## Surface kind changes

| Target | Change | Origin | Confidence | Status | Rationale |
|---|---|---|---|---|---|
| `getCustomer` | `tool` to `resource` | planner | 0.6 | applied | A read whose only inputs identify what to fetch is addressable, so a resource avoids spending a tool slot on a lookup. |
| `listCustomerOrders` | `tool` to `resource` | planner | 0.6 | applied | A read whose only inputs identify what to fetch is addressable, so a resource avoids spending a tool slot on a lookup. |

## Renames

| Target | Change | Origin | Confidence | Status | Rationale |
|---|---|---|---|---|---|
| `approveRefund` | `approveRefund` to `approve_refund_and_release_payment` | planner | 0.75 | applied | Derived from the operation summary 'Approve a refund and release the payment' rather than the source identifier 'approveRefund', which names a tool after the API instead of the task. |
| `createRefund` | `createRefund` to `create_refund_request` | planner | 0.75 | applied | Derived from the operation summary 'Create a refund request' rather than the source identifier 'createRefund', which names a tool after the API instead of the task. |
| `getCustomer` | `getCustomer` to `get_customer` | planner | 0.75 | proposed, not applied | Derived from the operation summary 'Get a customer' rather than the source identifier 'getCustomer', which names a tool after the API instead of the task. |
| `getCustomer` | `get_customer` to `look_up_customer` | human | 1.0 | applied | Name set by a reviewer in the overlay. |
| `listCustomerOrders` | `listCustomerOrders` to `list_customer_orders` | planner | 0.75 | applied | Derived from the operation summary 'List customer orders' rather than the source identifier 'listCustomerOrders', which names a tool after the API instead of the task. |

## Output projections

| Target | Change | Origin | Confidence | Status | Rationale |
|---|---|---|---|---|---|
| `createRefund` | `refund_id` | planner | 0.55 | applied | Projects the 1 field(s) the response declares required and drops status, so an agent is not handed the whole payload. Confirm nothing dropped is needed downstream. |
| `getCustomer` | `id`, `name` | planner | 0.55 | proposed, not applied | Projects the 2 field(s) the response declares required and drops email, internal_account_ref, so an agent is not handed the whole payload. Confirm nothing dropped is needed downstream. |

## Grouping

| Target | Change | Origin | Confidence | Status | Rationale |
|---|---|---|---|---|---|
| `approveRefund` | `-` to `refunds` | planner | 0.65 | applied | Grouped by the first path segment 'refunds', which is the coarsest grouping the specification states rather than one this planner invents. |
| `createRefund` | `-` to `refunds` | planner | 0.65 | applied | Grouped by the first path segment 'refunds', which is the coarsest grouping the specification states rather than one this planner invents. |
| `getCustomer` | `-` to `customers` | planner | 0.65 | applied | Grouped by the first path segment 'customers', which is the coarsest grouping the specification states rather than one this planner invents. |
| `listCustomerOrders` | `-` to `customers` | planner | 0.65 | applied | Grouped by the first path segment 'customers', which is the coarsest grouping the specification states rather than one this planner invents. |

## Approvals

| Target | Change | Origin | Confidence | Status | Rationale |
|---|---|---|---|---|---|
| `createRefund` |  | human | 1.0 | applied | Approved by a reviewer in the overlay, releasing it through the emission gate. |

## What a reviewer must decide

- **project** `getCustomer`: Projects the 2 field(s) the response declares required and drops email, internal_account_ref, so an agent is not handed the whole payload. Confirm nothing dropped is needed downstream.
- **rename** `getCustomer`: Derived from the operation summary 'Get a customer' rather than the source identifier 'getCustomer', which names a tool after the API instead of the task.

Decisions already accepted by a reviewer: 3.
