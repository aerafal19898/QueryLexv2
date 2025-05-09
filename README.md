# QueryLex v2 – Secure Legal‑RAG Marketplace  ⚖️🤖
Product to evolve.

QueryLex turns the single‑tenant “Rag‑Try” demo into an **EU‑hosted marketplace where legal
professionals buy, sell, and chat with Retrieval‑Augmented‑Generation (RAG) models**.
Creators upload encrypted document bundles, tag them by *Legal Topic* and *Jurisdiction*,
set a €/request price, and earn 70 % of revenue; the platform takes 30 % via Stripe Connect
destination charges.:contentReference[oaicite:0]{index=0}  
End‑users receive **3 free requests per month** (rate‑limited to 1 req/min) and then
top‑up credits (1 € = 1 000 tokens).:contentReference[oaicite:1]{index=1}  
Chats stream tokens in real time, cite source chunk hashes, and can be organised into
drag‑and‑droppable folders built with React‑Arborist.:contentReference[oaicite:2]{index=2}  
All documents and embeddings are Fernet‑encrypted and stored in an EU ChromaDB cluster,
with a ≥ 95 % recall/precision PII scrubber (Piiranha + regex) applied before indexing.:contentReference[oaicite:3]{index=3}

---

## ✨ Core features
* **Marketplace / Leaderboard** – usage + rating sort (OpenRouter style).:contentReference[oaicite:4]{index=4}
* **Creator Wizard** – upload ▶ tag ▶ price ▶ publish; immutable hash versioning.
* **Chat Workspace** – sidebar *Folder ▸ Chat* UI with context menus & keyboard shortcuts
  that meet WCAG AA.:contentReference[oaicite:5]{index=5}
* **Billing & Payouts** – Stripe Connect, full‑refund button for Admin.:contentReference[oaicite:6]{index=6}
* **GDPR / Security** – EU data locality + audit log.

---

## 🏗 Project structure
```text
backend/     # Flask (or FastAPI) API, Stripe webhooks, cron jobs
frontend/    # React + Vite + Tailwind UI
services/    # extraction, PII scrubber, embeddings
infra/       # Docker‑compose, Terraform, GitHub Actions
docs/        # architecture diagrams, this README
