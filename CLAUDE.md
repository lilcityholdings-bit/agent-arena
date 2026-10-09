# Company operating rules (apply to every session)

The owner is building a tech company made of several digital products and
plans to sell the company, or individual products, one day. Every piece of
work should keep each product easy to hand over to a buyer.

## Guide the owner
- Say so when a request would make a product harder to transfer or sell:
  personal accounts, mixed repos, secrets in code, unlicensed content, or
  work that isn't documented.
- Suggest the organized option first, and keep advice practical and short.

## Structure
- One product per GitHub repo, owned by the company GitHub organization
  (`lilcityholdings-bit`). Don't put a new product inside another
  product's repo.
- Every repo has a README.md (what it is, how to run it, how to deploy it)
  and a line in `docs/ASSETS.md` for each external account it depends on.
- Each product gets its own Railway project, named after the product.
- Accounts (YouTube, Railway, Stripe, domains, APIs) go under a company
  email, never a personal one, so they can be transferred.

## Code and data
- Secrets stay in environment variables and never go into git. Keep a
  `.env.example` listing the names.
- Record third-party licenses and content sources (for example, CC-BY
  attribution) so a buyer's due diligence can check them.
- Only use content the company owns or is licensed to use. Never automate
  anything that risks copyright strikes or account bans.
- Keep tests passing and commit with clear messages. The git history is
  part of what a buyer inspects.

# Company notes (read before working)

This section is the same in every company repo. When something here changes, update it in
every repo you can, in the same piece of work. Last updated October 2026.

## The founder
- A non-technical solo founder working only from a phone. Keep every message short and plain,
  and explain any technical word you have to use.
- Wants honesty over flattery. Say plainly when something is a bad idea, risky or not working.
  Report test results exactly as they came out.
- End each piece of work with: what's done, what's not, and what you need from the founder
  (a short numbered list).
- Never ask the founder to paste a key or password into the chat. Secrets go in the
  environment's settings as environment variables.

## Brand and products
- **Keptvow** is the company's brand (it replaces the name Ikenga). Live at https://keptvow.com.
- Three products launch together on **January 1, 2027**:
  - **Arena** (`agent-arena`): AI agents play each other. Tests agents.
  - **Keptvow trust** (`Agenttrust` repo, renamed Keptvow): settles disputes between agents and
    gives each a public trust score. Already live.
  - **Keptvow Clear**: usage billing for MCP servers and APIs that agents call. It checks the
    agent's Keptvow record, counts calls and sends monthly Stripe invoices from the service
    owner's own Stripe account. Code is in `clear/` in the `Ikenga` repo, branch
    `claude/ikenga-clear`. It should move to its own repo. Test mode only.
- Other company repos: `Ikenga` (the earlier bot exchange and bot-to-bot netting code),
  `discovery-bot` (Revenue Bots), `clip-engine` (Clip Engine).
- Clear's target customers are AI agent platforms and API/MCP owners, not crypto trading bots.

## Rules on top of the operating rules
- Never deploy to production or create Railway resources unless the founder says yes in that
  conversation.
- Work on a branch. Don't push to `main` without asking: some repos deploy from it.
- Test mode by default. No real money without the founder's explicit yes and a lawyer's review.
- Keptvow never holds or moves customer money. Customers pay sellers directly, into the
  seller's own Stripe account. If a Keptvow fee is ever collected through Stripe, only that
  fee reaches Keptvow.
- Never promise that anything is safe or guaranteed.

## Decisions made
- Clear is usage billing for MCP/API owners. It replaces the earlier bot-to-bot netting design.
- An agent's Keptvow record only counts in Clear once the link to its Keptvow id is confirmed.
- A coming-soon page for Clear is drafted (`clear/site/clear-body.html` in `Ikenga`). It is
  not on keptvow.com yet.

## Waiting on the founder (ask, don't assume)
1. **Clear pricing.** Options: A) 1¢ per paid bill; B) 2% of what's billed; C) monthly plans
   only; D) a mix: Free at 3% of what's billed, Pro $49/month + 1%, Scale $399/month + 0.5%.
   Advice given: D, launching with only the Free 3% plan. Not decided yet.
2. A separate repo for Clear (not created yet).
3. A Stripe test key in the environment, and `api.stripe.com` and `keptvow.com` allowed in the
   environment's network settings, so Clear can be tested against real Stripe.
4. Whether Keptvow should count Clear's payment reports toward agents' scores.
5. How many paid bills each plan includes before the heavy-usage rate.
6. A yes before anything goes on keptvow.com.

## Advice already given
- The biggest risk is building too many products at once. Advice: focus on Keptvow (with
  Clear) until there are 10 paying customers, and talk to one possible customer every week.
