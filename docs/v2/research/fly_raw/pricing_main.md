---
title: "Fly.io pricing"
description: "What Fly.io costs: per-second Machines pricing, metered Sprites usage, Managed Postgres cluster plans, support tiers and compliance."
---

# Fly.io pricing

Compute bills by the second, storage on what you provision. No plans to
pick up front on Machines or Sprites; Managed Postgres is sized by cluster
plan.

Canonical page: https://fly.io/pricing/
Sign up: https://fly.io/app/sign-up/

Prices in USD, excluding tax. Compute prices are for Ashburn (iad); other
regions vary — see https://docs.fly.io/reference/regions.

## Machines

Billed by the second while a Machine runs.

### Machines — compute

Every preset takes more RAM at $6.00 per GB per month, up to 128GB.

| Preset | Per hour | Per month |
| --- | --- | --- |
| shared-cpu-1x · 256MB | $0.0030 | $2.19 |
| shared-cpu-2x · 512MB | $0.0061 | $4.39 |
| shared-cpu-4x · 1GB | $0.0122 | $8.78 |
| shared-cpu-6x · 1.5GB | $0.0183 | $13.16 |
| shared-cpu-8x · 2GB | $0.0244 | $17.55 |
| performance-1x · 2GB | $0.0458 | $33.00 |
| performance-2x · 4GB | $0.0917 | $66.00 |
| performance-4x · 8GB | $0.1833 | $132.01 |
| performance-6x · 12GB | $0.2750 | $198.01 |
| performance-8x · 16GB | $0.3667 | $264.01 |
| performance-10x · 20GB | $0.4584 | $330.01 |
| performance-12x · 24GB | $0.5500 | $396.02 |
| performance-14x · 28GB | $0.6417 | $462.02 |
| performance-16x · 32GB | $0.7334 | $528.02 |
| Stopped Machine, per GB rootfs | — | $0.15 |

### Machines — compute by region

A region's price is the Ashburn price above times its multiplier, applied
to vCPU and RAM. Volumes and snapshots do not take this markup, and egress
is priced by destination rather than by the Machine's region.

| Region | Code | Multiplier | shared-cpu-1x · 256MB | performance-1x · 2GB |
| --- | --- | --- | --- | --- |
| Secaucus, NJ (US) | ewr | 1.000 | $2.19 | $33.00 |
| Ashburn, Virginia (US) | iad | 1.000 | $2.19 | $33.00 |
| Amsterdam, Netherlands | ams | 1.038 | $2.28 | $34.27 |
| Stockholm, Sweden | arn | 1.038 | $2.28 | $34.27 |
| Toronto, Canada | yyz | 1.115 | $2.45 | $36.81 |
| Paris, France | cdg | 1.135 | $2.49 | $37.44 |
| London, United Kingdom | lhr | 1.135 | $2.49 | $37.44 |
| Frankfurt, Germany | fra | 1.154 | $2.53 | $38.08 |
| San Jose, California (US) | sjc | 1.192 | $2.62 | $39.35 |
| Los Angeles, California (US) | lax | 1.200 | $2.63 | $39.59 |
| Dallas, Texas (US) | dfw | 1.250 | $2.74 | $41.25 |
| Chicago, Illinois (US) | ord | 1.250 | $2.74 | $41.25 |
| Singapore, Singapore | sin | 1.269 | $2.78 | $41.89 |
| Sydney, Australia | syd | 1.269 | $2.78 | $41.89 |
| Johannesburg, South Africa | jnb | 1.303 | $2.86 | $43.00 |
| Tokyo, Japan | nrt | 1.308 | $2.87 | $43.16 |
| São Paulo, Brazil | gru | 1.615 | $3.54 | $53.31 |

### Machines — storage and network

Volumes bill on provisioned capacity, not usage. First 10GB of snapshots
free each month. Every app gets a shared IPv4 and unlimited Anycast IPv6.

| Item | Price |
| --- | --- |
| Volumes, per GB / month | $0.15 |
| Snapshots, per GB / month | $0.08 |
| Egress, North America and Europe | $0.02 / GB |
| Egress, Asia Pacific, Oceania and South America | $0.04 / GB |
| Egress, Africa and India | $0.12 / GB |
| Dedicated IPv4 | $2.00 / mo |

### Machines — reserved compute

Reserve compute and save 40%. One-year blocks from $36/yr for shared,
$144/yr for performance. See
https://docs.fly.io/about/pricing#machine-reservation-blocks.

## Sprites

No plans and no tiers, and nothing charged per Sprite. CPU, memory and hot
storage are metered per hour of active use; a Sprite that exists but does
nothing costs nothing beyond its stored data.

### Sprites — compute

| Resource | Detail | Rate |
| --- | --- | --- |
| CPU time | Cumulative CPU usage measured by cpu.stat | $0.03825 / CPU-hour |
| Memory time | Actual memory usage | $0.021875 / GB-hour |

### Sprites — storage

Storage bills in GB-hours at two rates. Hot storage stops billing when the
Sprite goes to sleep; cold storage bills for as long as the data exists, so
a Sprite that is asleep all month still accrues it. Per-month figures are
the hourly rate over a 730-hour month.

| Tier | When it bills | Rate | Per GB-month |
| --- | --- | --- | --- |
| Hot | Bills while the Sprite is awake | $0.000683 / GB-hour | $0.50 |
| Cold | Bills for as long as you keep it | $0.000027 / GB-hour | $0.02 |

Estimate a workload at https://fly.io/calculator/.

## Managed Postgres

### Managed Postgres — cluster plans

Every plan includes high availability, automated backups and connection
pooling.

| Plan | CPU and RAM | Per month |
| --- | --- | --- |
| Basic | shared-2x · 1GB | $38 |
| Starter | shared-2x · 2GB | $72 |
| Launch | performance-2x · 8GB | $282 |
| Scale | performance-4x · 32GB | $962 |
| Performance | performance-8x · 64GB | $1,922 |

### Managed Postgres — storage

Billed on the storage your databases use, for a 30-day month, metered
hourly. Creating or deleting a cluster mid-month is prorated. Same-region
transfer is free.

| Item | Price |
| --- | --- |
| Database storage, per GB | $0.28 |
| Example: 10GB used | $2.80 / mo |
| Maximum per cluster | 1,000 GB |

## Support

Community support is free and always has been. The paid plans add an easily
nerd-sniped support team that feels like an extension of your own.

### Support — Standard, $29 / mo

Basic support for single developers and small teams. Great for personal projects and startups.

- 36-hour first response time
- Technical architecture support

### Support — Premium, $199 / mo

Enhanced support for growing businesses. Ideal for scaling applications and teams.

- 24-hour first response time
- 1-hour first response for urgent issues
- Dedicated Slack support channel
- Quarterly Solutions Architecture sessions

### Support — Enterprise, From $2,500 / mo

Top-tier support for large-scale operations. Perfect for mission-critical apps and enterprise deployments.

- 4-hour first response time, 24x7
- 15-minute emergency first response, 24x7
- Dedicated Slack support channel
- Quarterly Solutions Architecture sessions
- 99.9% uptime SLA

## Compliance

HIPAA package at $99/mo, with the BAA pre-signed. Available on any paid
plan. See https://fly.io/compliance/.

- SOC 2 Type II
- DPA available
- BAA pre-signed

## Startup program

Up to $15,000 in credit for eligible early-stage companies, plus a direct
line to our team. Apply at https://fly.io/startups/.

## Custom plan

Committed spend, volume discounts, invoicing and dedicated capacity.
Contact sales@fly.io.
