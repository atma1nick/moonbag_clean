# 🌙 MoonBag Bot

> The trading assistant that keeps you from your worst decisions.  
> Built for Solana meme coin traders who are tired of selling too early, holding too long, and forgetting their bags.

---

## The problem

Most meme traders don't lose because they pick bad tokens.  
They lose because of emotions.

- Sold at 3x. Token went to 80x.
- Held through the dump because "it'll recover."
- Forgot about that 0.2 SOL bag sitting at 40x for three months.
- Missed the whale entry because you weren't watching.

MoonBag is the cold, unemotional system you wish you had.

---

## What it does

**Set your exit plan once. Get alerted when it's time to act.**

You define your take-profit levels when you enter a trade — for example, sell 50% at 4x, 30% at 8x, keep the rest as a moonbag. MoonBag monitors your position 24/7 and pings you the moment each target is hit, with full PnL breakdown and one-tap confirmation.

That's it. No more watching charts at 3am.

---

## Features

### 📊 Position Tracker
Real-time PnL, current mcap, liquidity, volume, 1h/24h price change — all in one place. Every position shows exactly where you stand. Display in SOL or USD, your choice.

### 🎯 Take-Profit Alerts
You set the levels, the bot watches. When your target hits, you get an alert with your PnL, the suggested sell amount, and two buttons: **Done** (logs the sale, updates your position) or **Skip** (dismisses without acting). Edit your plan at any time without losing your progress.

### 🛑 Stop-Loss Alerts
Set a mcap floor on any position. If it drops there, you get an alert before the exit liquidity disappears. No automation — just a timely nudge so you make the call with a clear head.

### 🧠 Smart Wallet Monitor
Track wallets of known whales, insiders, and smart money. When they buy a token, you know. Add as many wallets as you want, label them anything you like.

### 🚨 Bundle Detector
When three or more of your tracked wallets buy the same token within the same hour, that's a signal. MoonBag fires a bundle alert so you can decide whether to follow before the chart moves.

### 🐦 KOL Monitor
Track crypto influencers on Twitter/X by handle. When they mention a Solana contract, you get the alert with the token details — before their followers finish reading the tweet.

### 📓 Trade Journal
Every closed trade and every taken take-profit is logged automatically. Winrate, total PnL, best exit multiple — your real numbers, not the ones you remember.

---

## Stack

Built on Python 3.11, python-telegram-bot, SQLAlchemy, Helius, and DexScreener. Runs on Railway.

---

## Roadmap

- [x] Position tracking with custom exit plans
- [x] Take-profit and stop-loss alerts
- [x] Smart wallet monitoring
- [x] Bundle detector
- [x] KOL Twitter monitor
- [x] Trade journal and stats
- [x] SOL / USD display
- [ ] Real-time WebSocket alerts via Helius
- [ ] Portfolio snapshot — shareable PnL image
- [ ] Smart wallet auto-discovery via Dune
- [ ] Telegram Mini App live dashboard
- [ ] $MOONBAG token — Pro access via hold or burn
- [ ] AI trade journal analysis

---

## $MOONBAG Token

Pro features will be unlocked by holding or burning $MOONBAG.  
Hold 5,000 tokens → free Pro as long as you hold.  
Burn tokens monthly → permanent Pro, reduces supply.

Token not yet launched. Stay tuned.

---

## License

MIT
