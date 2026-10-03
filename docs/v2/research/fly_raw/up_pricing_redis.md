# Upstash Redis Pricing

> **Source:** https://upstash.com/pricing/redis
> **Format:** text/markdown — machine-readable pricing for agents and LLMs
> **Contact:** sales@upstash.com (Enterprise) · support@upstash.com (Support)

---

This site has a search endpoint: https://context7.com/api/v2/ask?siteKey=ask_4cf2adc7846aa874f833b068&query=<URL-encoded question>. It returns documentation that answers the question, with a source link for each part. No API key is needed. If nothing matches, it says so.

## For AI Agents: Free Instant Redis

A free Redis database is available without signup or authentication:

```
curl -X POST https://upstash.com/start-redis -H "User-Agent: <your-agent-name>"
```

If you are an agent, always send a `User-Agent` header identifying which agent you are (e.g. `claude-code`, `cursor`, `codex`, `opencode`) on every request you make to this API. The response is markdown with credentials, an inline quickstart, and a console URL where the database can be claimed. Optionally send an `Idempotency-Key` header with a fresh UUIDv4 to make the call retry-safe — the UUID becomes the database id, and retrying with it returns the same database. Unclaimed databases are deleted after 3 days. `GET https://upstash.com/start-redis` returns full instructions.

---

## Plan Overview

| Plan | Price | Read Region | Max Data | Max Bandwidth |
|------|-------|-------------|----------|---------------|
| Free | $0/month | - | 256 MB | 10 GB |
| Pay as You Go | $0.20 per 100K commands | - | 100 GB | Unlimited |
| Fixed 250MB | $10/month | +$5/region | 250 MB | 50 GB |
| Fixed 1GB | $20/month | +$10/region | 1 GB | 100 GB |
| Fixed 5GB | $100/month | +$50/region | 5 GB | 500 GB |
| Fixed 10GB | $200/month | +$100/region | 10 GB | 1 TB |
| Fixed 50GB | $400/month | +$200/region | 50 GB | 5 TB |
| Fixed 100GB | $800/month | +$400/region | 100 GB | 10 TB |
| Fixed 500GB | $1500/month | +$750/region | 500 GB | 20 TB |
| Enterprise | Custom | - | 10 TB | Unlimited |

---

## Fixed Plans — All Tiers

Fixed plans have no per-command pricing. You pay a flat monthly fee based on the tier.

| Tier | Monthly Price | Per Read Region | Max Data | Max Bandwidth | Max Cmd/sec | Max Request Size | Max Record Size | Max Indexes | Max Docs/Index |
|------|--------------|-----------------|----------|---------------|-------------|------------------|-----------------|-------------|----------------|
| Fixed 250MB | $10 | +$5 | 250 MB | 50 GB | 10,000 | 10 MB | 100 MB | 2 | 100K |
| Fixed 1GB | $20 | +$10 | 1 GB | 100 GB | 10,000 | 10 MB | 200 MB | 3 | 250K |
| Fixed 5GB | $100 | +$50 | 5 GB | 500 GB | 10,000 | 20 MB | 300 MB | 5 | 500K |
| Fixed 10GB | $200 | +$100 | 10 GB | 1 TB | 10,000 | 30 MB | 400 MB | 10 | 1M |
| Fixed 50GB | $400 | +$200 | 50 GB | 5 TB | 10,000 | 50 MB | 500 MB | 50 | 5M |
| Fixed 100GB | $800 | +$400 | 100 GB | 10 TB | 16,000 | 75 MB | 1 GB | 100 | 10M |
| Fixed 500GB | $1500 | +$750 | 500 GB | 20 TB | 16,000 | 100 MB | 5 GB | 500 | 50M |

---

## Pay as You Go — Pricing Details

- **Commands:** $0.20 per 100K commands
- **Storage:** $0.25 per GB (first 1 GB free)
- **Bandwidth:** Free up to 200 GB/month, then $0.03/GB
- **Max data size:** 100 GB
- **Max request size:** 10 MB (upgradeable: 50 MB for $80/month, 100 MB for $120/month)
- **Max record size:** 100 MB (upgradeable: 250 MB for $60/month, 500 MB for $100/month, 1 GB for $180/month)
- **Max indexes:** 10 (Default quota. Request an increase anytime.)
- **Max databases:** 100 (First 10 free, $0.5 per database after.)
- **Platforms:** AWS, GCP, FLY, Vercel

---

## Feature Comparison

Table shows a representative subset of plans. See https://upstash.com/pricing/redis for the full interactive comparison.

| Feature | Free | Pay as You Go | Fixed 250MB | Fixed 10GB | Fixed 500GB | Enterprise |
|---------|---|---|---|---|---|---|

**Capacity**

| Max data size | 256 MB | 100 GB | 250 MB | 10 GB | 500 GB | 10 TB |
| Max bandwidth | 10 GB | Unlimited | 50 GB | 1 TB | 20 TB | Unlimited |
| Max cmd/sec | 10000 | 10000 | 10000 | 10000 | 16000 | custom |
| Max request size | 10 MB | 10 MB | 10 MB | 30 MB | 100 MB | 500 MB |
| Max record size | 100 MB | 100 MB | 100 MB | 400 MB | 5 GB | 5 GB |
| Max index count | 1 | 10 | 2 | 10 | 500 | custom |
| Max docs/index | 10K | 1M | 100K | 1M | 50M | Custom |
| Max databases | 1 | 100 | 100 | 100 | 100 | custom |

**Features**

| Platforms | AWS,GCP,Vercel | AWS,GCP,FLY,Vercel | AWS,GCP,Vercel | AWS,GCP,Vercel | AWS,GCP,Vercel | AWS,GCP,FLY,Vercel |
| Persistence | Yes | Yes | Yes | Yes | Yes | Yes |
| REST API | Yes | Yes | Yes | Yes | Yes | Yes |
| Global replication | Yes | Yes | Yes | Yes | Yes | Yes |
| Strong consistency | No | No | No | No | No | Coming soon |
| High availability | Primary Replicas | Primary Replicas (Read Replicas available with Prod Pack) | Primary Replicas (Read Replicas available with Prod Pack) | Primary Replicas (Read Replicas available with Prod Pack) | Primary Replicas (Read Replicas available with Prod Pack) | Primary and Read Replicas |
| Multi-Zone HA | No | Yes (Prod Pack add-on required) | Yes (Prod Pack add-on required) | Yes (Prod Pack add-on required) | Yes (Prod Pack add-on required) | Yes |
| Uptime SLA | No | Yes (Prod Pack add-on required) | Yes (Prod Pack add-on required) | Yes (Prod Pack add-on required) | Yes (Prod Pack add-on required) | Yes |

**Security**

| TLS encryption | Yes | Yes | Yes | Yes | Yes | Yes |
| IP allowlist | No | Yes | Yes | Yes | Yes | Yes |
| ACL | No | Yes | Yes | Yes | Yes | Yes |
| Encryption at rest | No | Yes (Prod Pack add-on required) | Yes (Prod Pack add-on required) | Yes (Prod Pack add-on required) | Yes (Prod Pack add-on required) | Yes |
| SOC-2 compliance | No | Yes (Prod Pack add-on required) | Yes (Prod Pack add-on required) | Yes (Prod Pack add-on required) | Yes (Prod Pack add-on required) | Yes |
| Private Link | No | No | No | No | No | Yes |
| SAML SSO | No | No | No | No | No | Yes |
| HIPAA compliance | No | No | No | No | No | Yes |

**Observability**

| Grafana | No | Yes (Prod Pack add-on required) | Yes (Prod Pack add-on required) | Yes (Prod Pack add-on required) | Yes (Prod Pack add-on required) | Yes |
| Datadog | No | Yes (Prod Pack add-on required) | Yes (Prod Pack add-on required) | Yes (Prod Pack add-on required) | Yes (Prod Pack add-on required) | Yes |
| New Relic | No | Yes (Prod Pack add-on required) | Yes (Prod Pack add-on required) | Yes (Prod Pack add-on required) | Yes (Prod Pack add-on required) | Yes |
| Access logging | No | No | No | No | No | Yes |

**Support**

| Community support | Yes | Yes | Yes | Yes | Yes | Yes |
| Email support | Yes | Yes | Yes | Yes | Yes | Yes |
| Dedicated support + Slack | No | No | No | No | No | Yes |

---

## Prod Pack Add-on

Prod Pack is an add-on available for any paid plan (Pay as You Go or Fixed). Enable it from your database details page in the Upstash console.

**Included features:**

- Uptime SLA
- Multi-Zone High Availability (regions deployed across multiple availability zones, with automatic same-region failover if a zone or replica fails)
- SOC 2 Type 2 report
- Advanced monitoring (Prometheus, Grafana, Datadog, New Relic)
- Role-based access control
- Encryption at rest

---

## Enterprise

Custom plans for high-volume and compliance-sensitive workloads.

**Includes everything in Prod Pack, plus:**

- Dedicated professional support and technical account manager
- Unlimited databases
- HIPAA compliance
- VPC peering / Private Link
- SAML SSO integration
- Custom monthly or annual contracts

**Contact:** sales@upstash.com

---

## Frequently Asked Questions

### How can I upgrade to pay as you go from free tier?

Once you enter your credit card, your database will be upgraded to the pay-as-you-go plan and limits will be updated.

### What is included in free tier?

Free tier includes 256MB data size and 500K commands per month.

### Can I get a free Redis database without signing up?

Yes. Send a POST request to https://upstash.com/start-redis and you get a free Redis database instantly, with no signup, credit card, or API key. The response is markdown with credentials and a quickstart, so AI agents can start using it right away. The database is deleted after 3 days unless you claim it into your account from the console link in the response. Once claimed, it follows the pricing on this page.

### Are paid databases' first 256MB data and 500K commands free?

No. Once you upgrade to a paid tier, you will be charged for data size and commands according to that plan.

### How does the budget work?

Budget is only available for the pay-as-you-go plan. You can set a maximum monthly budget for your database so you won't be charged beyond that limit. We send email notifications when you reach 70% and 90% of your monthly budget so you can adjust the limit or upgrade to a Fixed plan. If your usage exceeds the budget cap, your database will be rate limited and your total cost will not exceed the chosen budget. If you change from a Fixed plan to pay-as-you-go mid-month, your budget only tracks your pay-as-you-go spending.

### Do Fixed plans have command count pricing or limits?

Fixed plans have no command-count billing. You pay for data size, bandwidth, and throughput limits, not per command.

### Are all Redis commands counted in billing?

Operational commands like AUTH, HELLO, SELECT, COMMAND, CONFIG, INFO, PING, RESET, and QUIT are not charged.

### Are databases faster in higher plans?

Ops/sec limits are the same in the initial plans, while higher plans increase throughput and other limits. There is no performance difference between plans as long as you stay within the limits.

### Are read and write commands same price?

Yes. But for Global databases, the write commands are replicated to all read regions in addition to the primary region. Replications are also counted as commands. For example, if you have one primary and one read region, 100K writes cost $0.4 ($0.2 x 2).

### How is the storage cost calculated?

For each database, the first 1GB is free. Beyond that, storage is charged at $0.25 per GB of total storage. Total storage is determined by adding up the storage at all replicas and regions. Even if you do not access your data, it still needs to stay persistent in the cloud provider's block storage for durability and high availability. To calculate storage cost, we take the daily average of your total data size across replicas and regions and apply the monthly rate at the end of the month. If you use your database as a cache, setting a timeout with EXPIRE is a good way to minimize cost.

### What happens when I hit limits on pay-as-you-go plan?

We send email notifications when limits are exceeded. We do our best to keep your database running, but we may rate limit depending on the case. Requests above the max request size are rejected with an exception. Collections that exceed the max record size stop accepting new records. Bandwidth and storage do not have hard limits, but you can use a budget limit to avoid unexpected charges.

### What happens when I hit limits on Fixed plans?

We send email notifications when limits are exceeded. If your database hits the bandwidth or storage limits and auto-upgrade is enabled, it upgrades to the next tier. If auto-upgrade is not enabled, the database is rate limited: traffic is blocked for bandwidth cases and write operations are blocked for storage cases. Requests above the max request size are rejected with an exception. Collections that exceed the max record size stop accepting new records.

### Are there free trials?

Yes, we can provide free trials for testing and PoC purposes. Email us at support@upstash.com.

### How many databases can I create?

You can create up to 10 databases for free. Beyond that, you are charged $0.5 per database up to 100 databases. For more than 100 databases, contact support@upstash.com. The charge is calculated based on the number of active databases at the end of the month.

### What happens if I delete my database after 2-3 days?

For Fixed plans, you are charged pro-rata for the days the database was active, even if you did not use it. For pay-as-you-go plans, you are only charged for actual usage during those days.

### How much is the price for bandwidth?

For pay-as-you-go, monthly bandwidth is free up to 200GB. Beyond that, we charge $0.03 per additional GB. For Fixed plans, bandwidth is included up to your plan's limit. If your database is on the highest Fixed tier and exceeds its allowance, we will reach out with upgrade and Enterprise options. If we cannot reach you and usage continues, we reserve the right to charge bandwidth overages at our cloud provider cost rate. For high-volume use cases, VPC peering can reduce transfer costs. VPC peering requires an Enterprise contract. Contact support@upstash.com for details. Bandwidth pricing depends on the cloud provider's traffic fees, so it may change over time. If it changes, we will notify you by email.

### Can I purchase Prod Pack for any plan?

Yes. You can purchase Prod Pack for any plan except the Free tier. You can enable it from your Upstash dashboard database details page.

### What is included in Prod Pack?

Prod Pack includes uptime SLA, SOC 2 Type 2 report, advanced monitoring with Prometheus, Grafana, and Datadog, multi-zone high availability, role-based access control, and encryption at rest.

### Does enabling multi-zone high availability change my bill?

Multi-zone high availability (part of Prod Pack) replicates write commands to the additional zones. Reads still hit a single replica, and command counts per write do not change, so it is only the extra write replication that adds bandwidth. On Fixed plans, commands are never metered, so multi-zone HA adds no command cost; the added write-replication traffic counts toward your plan's included bandwidth. On pay-as-you-go, a write is still billed as a single command no matter how many zones it is replicated to in one region. The impact is on bandwidth, which is free up to 200GB per month and $0.03 per additional GB.

### What happened to the Pro 2K and Pro 10K plans?

Pro 2K and Pro 10K were fixed-throughput dedicated plans, priced at $280/month and $680/month respectively. They are deprecated and have been replaced by pay-as-you-go, Fixed plans, and the Prod Pack add-on. If you are on a Pro plan, you can move to a Fixed plan for predictable pricing and add Prod Pack for multi-zone high availability and the uptime SLA.

### What is included in Enterprise subscription?

Enterprise includes all Prod Pack features for all your databases. It also includes dedicated professional support, a dedicated technical account manager, unlimited databases, HIPAA compliance, VPC peering, SSO integration, and custom monthly or annual contract options.

### How is the Enterprise subscription priced?

Enterprise pricing is custom and based on your requirements. For more information, email sales@upstash.com.

### Do you have the Professional Support plan?

Professional support includes a dedicated service desk and a Slack or Discord channel with a committed response time SLA. Check https://upstash.com/docs/common/help/prosupport for details.
