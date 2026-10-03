> ## Documentation Index
> Fetch the complete documentation index at: https://docs.fly.io/llms.txt
> Use this file to discover all available pages before exploring further.

# Fly.io Resource Pricing

export const RegionPricingSelector = () => {
  const regions = [{
    code: "ams",
    city: "Amsterdam",
    country: "Netherlands",
    markup: 1.038461538
  }, {
    code: "iad",
    city: "Ashburn, Virginia (US)",
    country: "",
    markup: 1
  }, {
    code: "ord",
    city: "Chicago, Illinois (US)",
    country: "",
    markup: 1.25
  }, {
    code: "dfw",
    city: "Dallas, Texas (US)",
    country: "",
    markup: 1.25
  }, {
    code: "fra",
    city: "Frankfurt",
    country: "Germany",
    markup: 1.153846154
  }, {
    code: "jnb",
    city: "Johannesburg",
    country: "South Africa",
    markup: 1.302884615
  }, {
    code: "lhr",
    city: "London",
    country: "United Kingdom",
    markup: 1.134615385
  }, {
    code: "lax",
    city: "Los Angeles, California (US)",
    country: "",
    markup: 1.199519231
  }, {
    code: "cdg",
    city: "Paris",
    country: "France",
    markup: 1.134615385
  }, {
    code: "sjc",
    city: "San Jose, California (US)",
    country: "",
    markup: 1.192307692
  }, {
    code: "gru",
    city: "São Paulo",
    country: "Brazil",
    markup: 1.615384615
  }, {
    code: "ewr",
    city: "Secaucus, NJ (US)",
    country: "",
    markup: 1
  }, {
    code: "sin",
    city: "Singapore",
    country: "Singapore",
    markup: 1.269230769
  }, {
    code: "arn",
    city: "Stockholm",
    country: "Sweden",
    markup: 1.038461538
  }, {
    code: "syd",
    city: "Sydney",
    country: "Australia",
    markup: 1.269230769
  }, {
    code: "nrt",
    city: "Tokyo",
    country: "Japan",
    markup: 1.307692308
  }, {
    code: "yyz",
    city: "Toronto",
    country: "Canada",
    markup: 1.115384615
  }];
  const PRICE_PER_VCPU_SECOND = {
    shared: 0.0000008465,
    performance: 0.000012732
  };
  const INCLUDED_RAM_GB_PER_VCPU = {
    shared: 0.25,
    performance: 2
  };
  const RAM_PRICE_PER_GB_SECOND = 0.000002316;
  const presets = [{
    name: "shared-cpu-1x",
    cpu: 1,
    cpuType: "shared",
    tiers: ["256MB", "512MB", "1GB", "2GB"]
  }, {
    name: "shared-cpu-2x",
    cpu: 2,
    cpuType: "shared",
    tiers: ["512MB", "1GB", "2GB", "4GB"]
  }, {
    name: "shared-cpu-4x",
    cpu: 4,
    cpuType: "shared",
    tiers: ["1GB", "2GB", "4GB", "8GB"]
  }, {
    name: "shared-cpu-6x",
    cpu: 6,
    cpuType: "shared",
    tiers: ["1.5GB", "3GB", "6GB", "12GB"]
  }, {
    name: "shared-cpu-8x",
    cpu: 8,
    cpuType: "shared",
    tiers: ["2GB", "4GB", "8GB", "16GB"]
  }, {
    name: "performance-1x",
    cpu: 1,
    cpuType: "performance",
    tiers: ["2GB", "4GB", "8GB"]
  }, {
    name: "performance-2x",
    cpu: 2,
    cpuType: "performance",
    tiers: ["4GB", "8GB", "16GB"]
  }, {
    name: "performance-4x",
    cpu: 4,
    cpuType: "performance",
    tiers: ["8GB", "16GB", "32GB"]
  }, {
    name: "performance-6x",
    cpu: 6,
    cpuType: "performance",
    tiers: ["12GB", "24GB", "48GB"]
  }, {
    name: "performance-8x",
    cpu: 8,
    cpuType: "performance",
    tiers: ["16GB", "32GB", "64GB"]
  }, {
    name: "performance-10x",
    cpu: 10,
    cpuType: "performance",
    tiers: ["20GB", "40GB", "80GB"]
  }, {
    name: "performance-12x",
    cpu: 12,
    cpuType: "performance",
    tiers: ["24GB", "48GB", "96GB"]
  }, {
    name: "performance-14x",
    cpu: 14,
    cpuType: "performance",
    tiers: ["28GB", "56GB", "112GB"]
  }, {
    name: "performance-16x",
    cpu: 16,
    cpuType: "performance",
    tiers: ["32GB", "64GB", "128GB"]
  }];
  const ramGB = ram => ram.endsWith("MB") ? parseFloat(ram) / 1024 : parseFloat(ram);
  const basePricePerSecond = ({cpu, cpuType}, ram) => cpu * PRICE_PER_VCPU_SECOND[cpuType] + (ramGB(ram) - cpu * INCLUDED_RAM_GB_PER_VCPU[cpuType]) * RAM_PRICE_PER_GB_SECOND;
  const [selected, setSelected] = useState("iad");
  const region = regions.find(r => r.code === selected) ?? regions[0];
  const SECONDS_PER_HOUR = 3600;
  const SECONDS_PER_MONTH = 2592000;
  const BASELINE_RAM_PRICE_PER_30_DAYS = RAM_PRICE_PER_GB_SECOND * SECONDS_PER_MONTH;
  const currencyFormatter = minimumFractionDigits => new Intl.NumberFormat("en-US", {
    style: "currency",
    currency: "USD",
    minimumFractionDigits,
    maximumFractionDigits: 2
  });
  const monthFormatter = currencyFormatter(2);
  const ramFormatter = currencyFormatter(0);
  const formatPerSecond = value => `$${value.toFixed(8)}`;
  const formatPerHour = value => `$${value.toFixed(4)}`;
  const formatPerMonth = value => monthFormatter.format(value);
  return <>
      <p>
        <label htmlFor="region-select">Region: </label>
        <select id="region-select" value={selected} onChange={e => setSelected(e.target.value)} className="ml-2 rounded-md border border-gray-300 dark:border-gray-700 text-gray-900 dark:text-gray-100 px-3 py-1.5 shadow-sm focus:border-indigo-300 focus:outline-none focus:ring focus:ring-indigo-200 focus:ring-opacity-50">
          {regions.map(({code, city, country}) => <option key={code} value={code}>
              {city}
              {country ? `, ${country}` : ""} ({code})
            </option>)}
        </select>
      </p>

      <p>
        The price of a running <a href="/machines">Fly Machine</a> VM is the price of a named CPU/RAM preset, plus about{" "}
        {ramFormatter.format(BASELINE_RAM_PRICE_PER_30_DAYS * region.markup)} per 30 days per GB of additional RAM.
      </p>

      <p>Here’s the pricing for named presets and a few standard additional RAM configurations:</p>

      <table className="table-stripe">
        <thead>
          <tr>
            <th>Preset</th>
            <th>CPU(s)</th>
            <th>RAM</th>
            <th>Price/second</th>
            <th>Price/hour</th>
            <th>Price/month</th>
          </tr>
        </thead>
        <tbody>
          {presets.map(preset => preset.tiers.map((ram, tierIndex) => {
    const perSecond = basePricePerSecond(preset, ram) * region.markup;
    return <tr key={`${preset.name}-${ram}`}>
                  {tierIndex === 0 && <>
                      <td rowSpan={preset.tiers.length}>{preset.name}</td>
                      <td rowSpan={preset.tiers.length}>
                        {preset.cpu} {preset.cpuType}
                      </td>
                    </>}
                  <td>{ram}</td>
                  <td>{formatPerSecond(perSecond)}</td>
                  <td>{formatPerHour(perSecond * SECONDS_PER_HOUR)}</td>
                  <td>{formatPerMonth(perSecond * SECONDS_PER_MONTH)}</td>
                </tr>;
  }))}
        </tbody>
      </table>
    </>;
};

<img src="https://mintcdn.com/fly-io/izaS1l2UMuz6sm1H/images/pricing.png?fit=max&auto=format&n=izaS1l2UMuz6sm1H&q=85&s=9b865b44607c896bfcd26ee3316e766b" alt="Illustration by Annie Ruygt of Frankie the hot air balloon demonstrating a pricing chart on a whiteboard" width="2457" height="1323" data-path="images/pricing.png" />

**Fly.io bills for the resources you use, with no monthly platform fee. The smallest always-on app costs \$2.19 per 30 days. A small web app with a Managed Postgres database costs about \$44 a month. The table below lists what we charge for, followed by a few examples of what real-ish apps cost.**

## Everything we bill for

Prices are in USD. Machine prices in this table are for Ashburn, Virginia (`iad`) and Secaucus, NJ (`ewr`), our lowest-priced regions. Machine prices vary by region; see [Started Fly Machines](#started-fly-machines) for the full list.

Monthly Machine prices assume 30 days of continuous runtime. Machines are billed by the second while they're running, so an always-on Machine costs slightly more in a 31-day month.

| What you pay for | Price | Notes |
| - | - | - |
| Started Machine, `shared-cpu-1x` with 256MB RAM | \$2.19/month | Smallest Machine size. Billed by the second while running |
| Started Machine, `shared-cpu-1x` with 512MB RAM | \$3.69/month | |
| Started Machine, `shared-cpu-1x` with 1GB RAM | \$6.70/month | |
| Started Machine, `shared-cpu-2x` with 2GB RAM | \$13.39/month | |
| Started Machine, `performance-1x` with 2GB RAM | \$33.00/month | |
| Started Machine, `performance-2x` with 4GB RAM | \$66.00/month | |
| Additional Machine RAM | \$6.00/GB/month | RAM added beyond the amount included with the CPU preset |
| Stopped or suspended Machine | \$0.15/GB/month of rootfs | Only the Machine's root file system is billed while it's stopped |
| Volume | \$0.15/GB/month | Based on provisioned capacity, including while detached or attached to a stopped Machine |
| Volume snapshots | \$0.08/GB/month | First 10GB free each month. Based on data stored, not provisioned volume size |
| Dedicated IPv4 address | \$2/month | Optional. Apps get a shared IPv4 address and IPv6 addresses at no charge |
| Static egress IP | \$0.005/hour (\~\$3.60/month) | Optional |
| SSL certificates | \$0.10/month per single hostname; \$1/month per wildcard | First 10 single-hostname certificates are free |
| Outbound data to the internet | \$0.02/GB in North America and Europe<br />\$0.04/GB in Asia Pacific, Oceania, and South America<br />\$0.12/GB in Africa and India | Inbound data is free |
| Private network data between regions | \$0.006/GB in North America and Europe<br />\$0.015/GB in Asia Pacific, Oceania, and South America<br />\$0.050/GB in Africa and India | Data transfer within a region is free. Different rates apply to some older organizations; see [data transfer pricing](#data-transfer-pricing) |
| Managed Postgres | \$38/month (Basic) to \$1,922/month (Performance) | Storage is \$0.28/GB/month based on usage. See [Managed Postgres pricing](/postgres#pricing) |
| Support | \$29/month Standard; \$199/month Premium; Enterprise starts at \$2,500/month | Optional. Community support is free |
| Fly Kubernetes | \$75/month per cluster | Compute and volumes are billed separately |
| Extensions such as Tigris and Upstash | Provider's list price | Pricing is set by the extension provider and charges appear on your Fly.io invoice |

New organizations use Pay As You Go pricing. There's no monthly platform fee; you pay for the resources and services you use.

## What does a typical app cost?

Here are three example apps and what they'd cost per month. We use Ashburn (`iad`) for the examples unless otherwise noted, and assume the Machines run continuously. Traffic and storage marked "assumed" are example workloads, not included allowances. You can use the [pricing calculator](https://fly.io/calculator/) to estimate costs for your own app.

If your Machines [auto-stop](/launch/autostop-autostart) when they're idle, your compute costs can be lower. You pay for the time a Machine is running and for its rootfs while it's stopped. If the Machine runs all month, you'll pay the full always-on cost shown here. See [Cost Management](/about/cost-management) for more ways to estimate and control your bill.

### A small web app with Managed Postgres

| Item | Monthly cost |
| - | -: |
| One `shared-cpu-1x` 512MB Machine, running continuously | \$3.69 |
| 10GB volume | \$1.50 |
| Managed Postgres, Basic plan | \$38.00 |
| 1GB of Managed Postgres storage (assumed) | \$0.28 |
| Volume snapshots, within the 10GB free allowance (assumed) | \$0.00 |
| 10GB outbound data to the internet from North America (assumed) | \$0.20 |
| **Total** | **\$43.67** |

The app and database run in the same region, so data transfer between them is free. The app also uses a shared IPv4 address, which is free.

In this example, Managed Postgres accounts for most of the bill. The app Machine and its 10GB volume cost \$5.19 per month.

### A Discord bot or background worker running 24/7

| Item | Monthly cost |
| - | -: |
| One `shared-cpu-1x` 256MB Machine, running continuously | \$2.19 |
| 1GB outbound data to the internet from North America (assumed) | \$0.02 |
| **Total** | **\$2.21** |

This example doesn't use a volume, database, or dedicated IPv4 address, so there's nothing else to pay for. If the worker needs more memory, a `shared-cpu-1x` Machine with 512MB costs \$3.69 per month when running continuously.

### An app running in three regions

Suppose an app runs one 1GB Machine each in Ashburn (`iad`), Frankfurt (`fra`), and Singapore (`sin`). Each Machine serves 50GB of traffic to users and sends 10GB of private network traffic to Machines in other regions during the month.

| Item | Monthly cost |
| - | -: |
| One `shared-cpu-1x` 1GB Machine in Ashburn (`iad`) | \$6.70 |
| One `shared-cpu-1x` 1GB Machine in Frankfurt (`fra`) | \$7.73 |
| One `shared-cpu-1x` 1GB Machine in Singapore (`sin`) | \$8.50 |
| 100GB outbound data from `iad` and `fra` at \$0.02/GB (assumed) | \$2.00 |
| 50GB outbound data from `sin` at \$0.04/GB (assumed) | \$2.00 |
| 20GB cross-region private network data from `iad` and `fra` at \$0.006/GB (assumed) | \$0.12 |
| 10GB cross-region private network data from `sin` at \$0.015/GB (assumed) | \$0.15 |
| **Total** | **\$27.20** |

Machine prices vary by region, which accounts for the different compute costs above.

Cross-region private network traffic is billed in both directions. The request is billed at the rate for the region it leaves, and the response at the rate for the region it leaves. So if a small request returns a large response, most of the cost is at the responding region's rate.

This example uses granular data transfer pricing. Organizations created before July 18, 2024 that haven't switched to granular pricing pay the public internet rate for cross-region traffic instead. See [data transfer pricing](#data-transfer-pricing).

## Is there a free tier?

No. New organizations don't have a free tier or a monthly free usage allowance.

New accounts do get a [free trial](/about/free-trial), which lasts for up to 2 hours of Machine runtime or 7 days, whichever comes first. You don't need a credit card to start the trial. After the trial ends, you'll need to add a credit card to keep using billable resources.

The cheapest always-on app is a `shared-cpu-1x` Machine with 256MB of RAM in Ashburn (`iad`) or Secaucus (`ewr`). It costs \$2.19 per 30 days of continuous runtime, plus any outbound data transfer (\$0.02/GB in North America and Europe).

Some resources and allowances are free even after the trial ends. These include the first 10GB of volume snapshot storage each month, the first 10 single-hostname SSL certificates, inbound data transfer, shared IPv4 addresses, and IPv6 addresses.

Organizations on plans discontinued in October 2024 keep the free allowances included with those plans. See [Discontinued Plans](/about/discontinued-plans).

## How it works

Fly.io services are billed per organization, with [Linked Organizations](/about/billing#unified-billing) reporting resource usage to their parent Billing Organization. Plans get complicated, so we just charge based on usage. Pick and choose which pieces you need for your application; that's all you'll see on your invoice.

Organizations are administrative entities on Fly.io that let you add members, share app development environments, and manage billing. [Billing](/about/billing) is based on the resources provisioned for your apps, pro-rated for the time they are provisioned.

Organizations may be subject to automated scaling limits to prevent abuse or to help with capacity planning. Email the address in the error message if you run into such a limit and it's getting in your way.

All organizations (except for Linked Organizations) require a [credit card](/about/billing#payment-options) on file.

## Compute

We charge for started and stopped Machines differently. For more details about how costs are calculated, see [Machine billing](/about/billing#machine-billing). To understand the difference between `performance` and `shared` CPU types in Machines, see [CPU performance](/machines/cpu-performance).

### Started Fly Machines

<RegionPricingSelector />

### Stopped Fly Machines

For stopped Machines we charge only for the root file system (rootfs) needed for each Machine. Each 1GB of rootfs for a Machine stopped for 30 days is \$0.15. The amount of rootfs needed is defined by your OCI image generated on your app plus a few [containerd](https://containerd.io/) tweaks on the underlying file system.

### Machine reservation blocks

You'll get a 40% discount when you reserve a block of compute time, either for `performance` Machines or `shared` Machines, in a specific region.
Reservations apply to any number of Machines of the specified CPU class, in the specified region, in any of your organisation's apps.

The available reservation sizes are:

**Performance Machines**

* \$144/year for \$20/month of usage
* \$1,440/year for \$200/month of usage
* \$14,400/year for \$2,000/month of usage

**Shared Machines**

* \$36/year for \$5/month of usage
* \$360/year for \$50/month of usage
* \$3,600/year for \$500/month of usage

You pay the "per year" amount upfront, and each month receive a credit worth the "per month" amount. The credit does not rollover; it's only valid for the month in which it's granted. The credit applies only to CPU and additional RAM charges.

There's no limit on the number or combinations of blocks that can be purchased. Reservations are backdated to the first day of the month in which they're purchased.

For example, if you purchase a \$36/year `shared` Machines block in `cdg`, you'll pay \$36 upfront and receive \$5/month of credits applicable to `shared` Machines in `cdg` for 12 months, starting with the month of the purchase. Amortised over 12 months, the \$36 upfront cost is \$3/month, which is a 40% discount on the \$5/month of credits you receive.

You can set up reservations via self-service in the billing section of your Fly.io [dashboard](https://fly.io/dashboard). They apply to usage starting on the 1st, so setting up reservations any time in the month will give you the credits the entire month.

## Managed Postgres

The price of running Fly.io Managed Postgres depends on your selected Managed Postgres Plan and the amount of storage your databases use.

Current pricing for Managed Postgres plans and storage is available [here](/postgres#pricing).

<Warning>
  **Important:** Managed Postgres lives outside your apps. Deleting an app won’t delete its database. Have a look in your Dashboard when you're cleaning up. A quick check can save you a surprise charge later.
</Warning>

## Persistent Storage Volumes

### Volumes

[Fly Volumes](/volumes) are local persistent storage for Machines.

* \$0.15/GB per month of provisioned capacity

[Volume billing](/about/billing#volume-billing) is pro-rated to the hour.

You'll be charged for volumes that you create, whether they are attached to a Machine or not, including when an attached Machine is stopped.

### Volume Snapshots

<Info>
  **New charges**

  <p className="mt-2">
    Starting January 1st 2026, we're introducing charges for [volume snapshot](/volumes/snapshots) storage. You'll see the first charges on the invoice issued at the start of February 2026.
  </p>

  <p className="mt-2">
    If you're an existing customer, you can check your usage in the **Billing** section of the [dashboard](https://fly.io/dashboard/personal/billing) on your Upcoming Invoice and in the Cost Explorer.
  </p>
</Info>

* \$0.08/GB per month
* First 10GB free each month

[Volume Snapshot billing](/about/billing#volume-snapshot-billing) is pro-rated to the hour.

Automatic daily snapshots with 5 days retention are enabled by default on new volumes. This can be [adjusted](/volumes/snapshots#set-or-change-the-snapshot-retention-period) or [disabled](/volumes/snapshots#disable-automatic-daily-snapshots).

Usage is calculated based on the total stored size of the snapshots, not the provisioned volume size. You're only charged for the actual data stored - if you've written 1GB to a 10GB volume, you'll be charged for around 1GB of snapshot storage.

Snapshots for each volume are stored incrementally, so you'll only be charged for data that has changed since the previously stored snapshot.

## Network prices

### Anycast IP addresses

Each application receives a [shared IPv4 address](/networking/services#shared-ipv4) and unlimited [Anycast IPv6](/networking/services#ipv6) addresses for global load balancing.

Dedicated IPv4 addresses are \$2/mo.

### Managed SSL certificates

We use Let's Encrypt to issue certificates, and donate half of our SSL fees to them at the end of each calendar year.

* Single hostname certificates: \$0.10/mo
* Wildcard certificates: \$1/mo

Every organization's first 10 single hostname certificates are free.

### Data transfer pricing

We bill for data leaving your app destined for the public internet or for apps or Machines in other regions, including:

* Data egress to the Internet, from Machine to edge server to Internet
* Data transfer over private network between regions, from Machine to edge server and edge server to Machine
* Data transfer to some extensions like Upstash Redis

The following types of traffic are free:

* All inbound data transfer
* Data transfer between apps or Machines in the same region (for organizations using granular data transfer rates)
* Data transfer from apps without an assigned IP address (for organizations not using granular data transfer rates)

Fly.io pricing is per region group for outbound data transfer. You'll see a more detailed breakdown of cost per region and per traffic type on your monthly invoice.

<Info>
  **Important:** Organizations created after July 18 2024 are automatically opted-in to use the granular data transfer rates and are billed at a different rate for private network data transfer between regions, per the following table. Organizations not using granular data transfer rates are billed for all data transfer (excluding that listed as free above) at the "Egress to public internet" rate.
</Info>

| Region groups | Egress to public internet cost | Private network cross-region transfer cost |
| - | - | - |
| - North America<br />- Europe | \$0.02 per GB | \$0.006 per GB |
| - Asia Pacific<br />- Oceania<br />- South America | \$0.04 per GB | \$0.015 per GB |
| - Africa<br />- India | \$0.12 per GB | \$0.050 per GB |

To opt-in to granular bandwidth pricing, go to the [**Organizations** page](https://fly.io/organizations) in the dashboard, click the organization name to change, then click **Switch to granular bandwidth pricing**. You won't be able to return to using the non-granular data transfer rates once you opt in.

### Static Egress IPs for Machines

Static egress IPs for Machines provide dedicated outbound IP addresses for your Machines. When you allocate a static egress IP, you'll get both an IPv4 and IPv6 address for this single price.

* \$0.005 per hour (\~\$3.60/month)
* Machines do not have a static IP by default

## Support

[Community support](https://community.fly.io/) is included for all customers, regardless of usage level.
You can get access to a support plan by purchasing a Standard (\$29/month), Premium (\$199/month), or Enterprise (starting at \$2500/month) package in the **Support** section of your dashboard. For more about Support, see [Support at Fly.io](/about/support).

## Fly Kubernetes

[Fly Kubernetes](/kubernetes) (FKS) is a managed Kubernetes service that runs on Fly.io.

* \$75/month per cluster
* Plus the cost of [compute](#compute) and [Fly volumes](#persistent-storage-volumes) that you create

## Extensions

Fly.io offers managed services operated by third parties, such as [Tigris Object Storage](/tigris) and [Upstash Redis](/upstash/redis).

When you provision their services, you become their customer, and you pay their list prices via your monthly Fly.io bill. Charges are updated daily in your Fly.io dashboard.

You will not be billed separately for:

* Machines running the services, which are hosted in the provider's account
* IP addresses associated with the service

You **will** be billed separately for data transfer to these external third-party services, including Tigris Object Storage. See our [data transfer pricing](#data-transfer-pricing) for details.

## Unsupported Products

### Unmanaged Fly Postgres (Unsupported)

[Fly Postgres](/unmanaged-postgres) is a PostgreSQL database that you create and then manage yourself. Fly Postgres clusters are Fly Apps that consist of Machines, volumes, and any configured extra memory.

The [Machine price](#compute) and [volume price](#persistent-storage-volumes) for Fly Postgres are the same as any other Machine and volume you'd run on Fly.io. Assuming the Machines are running all the time, the cost for the preset configurations is about \$2/month for a single node cluster for dev projects and from about \$82 to \$164/month for a three-node production cluster. You don't need to keep the preset configurations, you can [scale your Fly Postgres Machines](/unmanaged-postgres/managing/scaling) to suit your workload at any time.

## Legacy plans

On a legacy plan and wondering what that includes? Read more about [Discontinued Plans](/about/discontinued-plans).

## **Related reading**

* [Billing for Fly.io](/about/billing) How invoicing, payment methods, and usage tracking work.
* [Cost Management](/about/cost-management) Best practices for estimating, monitoring, and controlling your spend.
* [Free Trial](/about/free-trial) What the Fly.io Free trial gives you and when billing begins.
* [Organization Roles & Permissions](/security/org-roles-permissions) How org structure, permissions and billing interplay.
* [Optimize Compute Costs: Fine‑tune your app](/apps/fine-tune-apps) Tuning machine size, memory/CPU, and stop‑/start behavior to reduce waste.
