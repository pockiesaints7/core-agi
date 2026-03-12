# BINANCE + CORE AGI — Full Business Integration Guide

**Last updated:** 2026-03-12  
**Scope:** Two separate Binance systems. Both matter. Both serve different purposes.

---

## THE BIG PICTURE

```
BINANCE ECOSYSTEM FOR CORE AGI
│
├── SYSTEM 1 — BINANCE PAY (Merchant API)
│   └── CORE accepts/sends crypto payments
│       ├── C2B: Customer pays Ki (products, services, subscriptions)
│       └── C2C: CORE sends crypto to others (payouts, transfers)
│
└── SYSTEM 2 — BINANCE AI AGENT SKILLS (Skills Hub)
    └── CORE trades and monitors markets autonomously
        ├── Binance Spot — execute real trades, place orders
        ├── Query Address Info — whale/wallet tracking
        ├── Query Token Info — token due diligence
        ├── Crypto Market Rank — trending assets, smart money
        ├── Meme Rush — meme token lifecycle monitoring
        ├── Smart Money Signals — large wallet buy/sell signals
        └── Contract Risk Detection — smart contract audit
```

Key distinction:
- Binance Pay = CORE as a BUSINESS (accepting money from customers)
- Agent Skills = CORE as a TRADER/ANALYST (interacting with markets)

---

## PART 1 — BINANCE PAY (Revenue Layer)

### What it is

Binance Pay is crypto payment infrastructure — like Stripe but crypto-native.
Dominates Southeast Asia and Indonesia. Zero tx fees. 200+ currencies. QR code or checkout URL.

### Full API capability map

| Endpoint | What it does | CORE use case |
|---|---|---|
| POST /binancepay/openapi/v3/order | Create payment order | Customer buys service/product |
| POST /binancepay/openapi/v2/order/query | Query order status | Check if payment completed |
| POST /binancepay/openapi/v3/order/close | Close order | Cancel unpaid order |
| POST /binancepay/openapi/v2/refund | Issue refund | Refund to customer |
| POST /binancepay/openapi/transfer | C2C transfer | CORE sends crypto to another user |
| POST /binancepay/openapi/direct-debit/contract | Create subscription | Recurring billing |
| POST /binancepay/openapi/payout/transfer | Payout | Bulk payments to multiple recipients |
| GET /binancepay/openapi/certificates | Get public key | Verify webhook signatures |
| GET /binancepay/openapi/balance | Check wallet balance | How much in merchant wallet |

### Payment flow (C2B)

```
1. CORE calls POST /binancepay/openapi/v3/order
   → gets: prepayId, checkoutUrl, qrContent, universalUrl

2. Ki shows customer: QR code (Binance app scan) OR checkoutUrl (web) OR universalUrl (mobile)

3. Customer pays in Binance app

4. Binance fires webhook → CORE's /webhook/binance-pay
   bizType=PAY, bizStatus=PAY_SUCCESS

5. CORE verifies signature → marks paid in Supabase → sends Telegram alert
   → returns HTTP 200 + body "SUCCESS" (Binance retries 6x if missing)
```

### Create order v3 required fields

```python
{
  "env": {"terminalType": "WEB"},
  "merchantTradeNo": "CORE_1741234567_001",  # unique, alphanumeric, max 32
  "orderAmount": "10.00",
  "currency": "USDT",
  "description": "CORE AGI Consultation",    # NEW required in v3
  "goodsDetails": [{
    "goodsType": "02",          # 02 = virtual goods
    "goodsCategory": "Z000",    # Z000 = others
    "referenceGoodsId": "consult_1h",
    "goodsName": "1 Hour CORE AGI Consultation",
    "goodsUnitAmount": {"currency":"USDT","amount":"10.00"}
  }],
  "returnUrl": "https://core-agi-production.up.railway.app/pay/success",
  "cancelUrl": "https://core-agi-production.up.railway.app/pay/cancel",
  "webhookUrl": "https://core-agi-production.up.railway.app/webhook/binance-pay"
}
```

### Authentication (HMAC-SHA512)

```python
async def binance_pay_request(endpoint: str, payload: dict) -> dict:
    ts = str(int(time.time() * 1000))
    nonce = ''.join(random.choices(string.ascii_letters, k=32))
    body = json.dumps(payload)
    to_sign = f"{ts}\n{nonce}\n{body}\n"
    sig = hmac.new(BINANCE_PAY_SECRET.encode(), to_sign.encode(), hashlib.sha512
                  ).hexdigest().upper()
    headers = {
        "Content-Type": "application/json",
        "BinancePay-Timestamp": ts,
        "BinancePay-Nonce": nonce,
        "BinancePay-Certificate-SN": BINANCE_PAY_KEY,
        "BinancePay-Signature": sig
    }
    async with httpx.AsyncClient() as c:
        r = await c.post(f"https://bpay.binanceapi.com{endpoint}", headers=headers, content=body)
    return r.json()
```

### Webhook verification (security-critical)

```python
@app.post("/webhook/binance-pay")
async def handle_binance_pay_webhook(request: Request):
    raw_body = await request.body()
    # Verify: payload = timestamp + "\n" + nonce + "\n" + rawBody + "\n"
    # base64_decode(BinancePay-Signature) verified against SHA256 with RSA PKCS1v15
    # Public key from: GET /binancepay/openapi/certificates (cache it)
    data = json.loads(raw_body)
    if data.get("bizStatus") == "PAY_SUCCESS":
        order_data = json.loads(data.get("data", "{}"))
        trade_no = order_data.get("merchantTradeNo")
        supabase.table("payments").update(
            {"status":"paid","webhook_data":data,"paid_at":"now()"}
        ).eq("merchant_trade_no", trade_no).execute()
        # send Telegram alert to Ki
    return Response(content="SUCCESS", status_code=200)
```

### Subscriptions / Direct Debit

Add `directDebitContract` object to create order call.
Scenarios: SUBSCRIPTION (monthly), PAY_AS_YOU_GO, INSTALLMENT.
After contract created, CORE charges customer automatically each cycle.
Whitelist required — request from Binance merchant support.

### Indonesia notes

- Binance Pay widely used locally — strong adoption
- No fiat conversion — crypto stays crypto in merchant wallet
- Pair with Midtrans (IDR) for full coverage = IDR + crypto
- KYC required — individual merchant OK, not just businesses

---

## PART 2 — BINANCE AI AGENT SKILLS (Market Intelligence + Trading)

Source: https://github.com/binance/binance-skills-hub (official, launched Q1 2025)
Same SKILL.md format as CORE's own skill system. Works with Claude natively.

### 7 skills breakdown

**SKILL 1 — BINANCE SPOT (CEX Trading)**
Auth: YES — API Key + Secret (IP-restricted to Railway)
Can do: Real-time market data (ticker, order book, candlesticks), execute trades (market/limit/OCO/OPO/OTOCO), cancel orders, check balances, trade history
CRITICAL: Always require "CONFIRM" from Ki before any mainnet trade
Testnet: testnet.binance.vision — always test here first

**SKILL 2 — QUERY ADDRESS INFO (Whale Tracking)**
Auth: NO (public APIs)
Can do: Analyze any wallet — holdings, valuation, 24h changes, concentration
Use case: Track smart money wallets. Know when whales accumulate.

**SKILL 3 — QUERY TOKEN INFO (Due Diligence)**
Auth: NO (public)
Can do: Token metadata, price, market cap, liquidity, top holders, volume, buy/sell pressure
Use case: Run before buying ANY token. Instant snapshot.

**SKILL 4 — CRYPTO MARKET RANK (Market Intelligence)**
Auth: NO (public)
Can do: Trending tokens, smart money inflows, social hype, top trader PnL, Binance Alpha list
API: https://web3.binance.com/bapi/defi/v1/public/...
Use case: Daily morning briefing — what's hot, what's smart money buying

**SKILL 5 — MEME RUSH (Meme Token Monitoring)**
Auth: NO (public)
Can do: Meme token creation rate, migration events, top 100 by breakout score, narrative stages
Use case: Catch meme coins early. CORE monitors 24/7, alerts on breakout patterns.

**SKILL 6 — SMART MONEY SIGNALS**
Auth: NO (public)
Can do: Large wallet buy/sell signals real-time, filter by chain/token/wallet type, inflow/outflow
Use case: Never trade against smart money. CORE alerts when institutions move.

**SKILL 7 — CONTRACT RISK DETECTION (Token Audit)**
Auth: NO (public)
Can do: Honeypot check, mint/freeze authority, ownership concentration, buy/sell tax, LP lock
Use case: MANDATORY before buying any new token. CORE auto-audits on request.

### Integration with CORE

Skills 2-7 = public APIs, callable TODAY via web_fetch. Zero setup.
Skill 1 = needs API keys in Cloudflare vault + Python code in core.py.

When Ki says "what's trending?" → CORE calls Crypto Market Rank API → reports results
When Ki says "audit this contract 0x..." → CORE calls Contract Risk Detection → reports
When Ki says "buy 10 USDT of BNB" → CORE calls Spot API → asks for CONFIRM → executes

---

## PART 3 — FULL ARCHITECTURE

```
CORE AGI
│
├── BUSINESS LAYER (Binance Pay)
│   ├── /webhook/binance-pay       ← Railway endpoint
│   ├── create_payment_order()     ← generates QR/checkout
│   ├── payments table (Supabase)  ← all transactions
│   └── Telegram alert             ← Ki notified on payment
│
├── INTELLIGENCE LAYER (Skills 2-7, public APIs)
│   ├── web_fetch → Binance Web3 API endpoints (no auth)
│   ├── Daily: morning briefing (trending + smart money)
│   ├── On alert: meme breakout detected
│   └── On request: token audit
│
└── TRADING LAYER (Skill 1, needs keys)
    ├── spot_trade() in core.py     ← buy/sell with CONFIRM gate
    ├── get_portfolio()             ← read-only balance check
    └── trades table (Supabase)    ← trade log
```

### Supabase tables needed

```sql
CREATE TABLE payments (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  merchant_trade_no TEXT UNIQUE NOT NULL,
  prepay_id TEXT, status TEXT DEFAULT 'pending',
  amount DECIMAL(18,8), currency TEXT, description TEXT,
  customer_ref TEXT, webhook_data JSONB,
  created_at TIMESTAMPTZ DEFAULT now(), paid_at TIMESTAMPTZ
);

CREATE TABLE market_signals (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  signal_type TEXT, token_symbol TEXT, chain TEXT,
  data JSONB, created_at TIMESTAMPTZ DEFAULT now()
);

CREATE TABLE trades (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  order_id TEXT, symbol TEXT, side TEXT,
  quantity DECIMAL(18,8), price DECIMAL(18,8),
  status TEXT, confirmed_by TEXT DEFAULT 'ki',
  created_at TIMESTAMPTZ DEFAULT now()
);
```

---

## PART 4 — WHAT KI MUST DO MANUALLY

### Binance Pay (accept payments)

1. Apply: https://merchant.binance.com/en — KYC, submit merchant application (individual OK)
   Wait 1-3 business days for approval
2. After approval: Developers → Settings → API Keys → Generate API Key
   Copy IMMEDIATELY: API Key + Secret Key (secret shown once only)
3. Set webhook: Developers → Webhooks → Edit → enter Railway URL
4. Add to Cloudflare vault: BINANCE_PAY_API_KEY, BINANCE_PAY_SECRET_KEY
5. Ask Claude to implement /webhook/binance-pay endpoint in core.py

### Binance Spot Trading (execute trades)

1. binance.com → Profile → API Management → Create API → "System-generated key"
   Name: CORE AGI Spot
2. Permissions: Enable Reading + Enable Spot & Margin Trading
   IP restriction: YES — Railway IP from Railway dashboard → Settings
   Do NOT enable: Withdrawals, Futures, Options
3. Recommended: create testnet key first at testnet.binance.vision
4. Add to Cloudflare vault: BINANCE_API_KEY, BINANCE_SECRET_KEY

### Intelligence skills (no setup needed)

Skills 2-7 use public APIs — CORE can call them TODAY. Zero config.

---

## PART 5 — CAPABILITIES

### Today (zero setup)

- Token audit: "CORE, audit this contract 0x..."
- Market trends: "CORE, what's trending today?"
- Whale tracking: "CORE, analyze this wallet"
- Meme monitoring: "CORE, what memes are breaking out?"
- Smart money: "CORE, what's smart money buying on BNB Chain?"

### After Binance Pay setup

- Generate payment orders: "CORE, create payment for 50 USDT, description: CORE consultation"
- Check payment: "CORE, has order CORE_12345 been paid?"
- Telegram alerts when any payment lands

### After Spot API setup

- Portfolio check: "CORE, what's my Binance balance?"
- Price check: "CORE, what's BNB price?"
- Trade execution: "CORE, buy 10 USDT of BNB" → CONFIRM gate → executes

---

## PART 6 — RISK RULES (non-negotiable)

Trading:
- NEVER trade without Ki typing "CONFIRM"
- Max 10% of portfolio per single position
- Spot only — no margin, no futures, no leverage
- Audit every new token before buying
- Testnet first for any new trading code
- API key NEVER has withdrawal permission

Payments:
- NEVER fulfill order without verified webhook signature
- Idempotency: merchantTradeNo is dedup key
- No auto-refunds — require Ki approval
- Store all raw webhook data in Supabase

---

## CREDENTIALS SUMMARY

```
BINANCE_PAY_API_KEY       ← merchant.binance.com → after KYC approval
BINANCE_PAY_SECRET_KEY    ← same
BINANCE_API_KEY           ← binance.com → API Management
BINANCE_SECRET_KEY        ← same
BINANCE_TESTNET_KEY       ← testnet.binance.vision (recommended)
BINANCE_TESTNET_SECRET    ← same
```

Sources:
- https://developers.binance.com/docs/binance-pay/api-order-create-v3
- https://github.com/binance/binance-skills-hub
- https://merchant.binance.com/en
- https://testnet.binance.vision

Last updated: 2026-03-12 | Owner: REINVAGNAR (Ki)
