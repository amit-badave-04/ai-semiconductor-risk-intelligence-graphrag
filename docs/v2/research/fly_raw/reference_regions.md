> ## Documentation Index
> Fetch the complete documentation index at: https://docs.fly.io/llms.txt
> Use this file to discover all available pages before exploring further.

# Regions

<img src="https://mintcdn.com/fly-io/izaS1l2UMuz6sm1H/images/regions-new.png?fit=max&auto=format&n=izaS1l2UMuz6sm1H&q=85&s=6f6e61f0f8d6890f907b56f2ea688563" alt="Illustration by Annie Ruygt of servers in different regions with map markers" width="1200" height="709" data-path="images/regions-new.png" />

Fly.io runs applications physically close to users: in datacenters around the world, on servers we run ourselves. You can deploy apps in regions worldwide, so your users in Tokyo, São Paulo, or Amsterdam connect to the nearest server through our global Anycast network.

<Info>
  Run <code>fly platform regions</code> to get a list of regions.
</Info>

## Fly.io Regions

You can host your apps in any of the following regions.

| Region ID | Region Location | Gateway | MPG |
| - | - | - | - |
| ams | Amsterdam, Netherlands | ✓ | ✓ |
| arn | Stockholm, Sweden | ✓ | |
| cdg | Paris, France | ✓ | |
| dfw | Dallas, Texas (US) | ✓ | ✓ |
| ewr | Secaucus, NJ (US) | ✓ | |
| fra | Frankfurt, Germany | ✓ | ✓ |
| gru | Sao Paulo, Brazil | | ✓ |
| iad | Ashburn, Virginia (US) | ✓ | ✓ |
| jnb | Johannesburg, South Africa | | |
| lax | Los Angeles, California (US) | ✓ | ✓ |
| lhr | London, United Kingdom | ✓ | ✓ |
| nrt | Tokyo, Japan | ✓ | ✓ |
| ord | Chicago, Illinois (US) | ✓ | ✓ |
| sin | Singapore, Singapore | ✓ | ✓ |
| sjc | San Jose, California (US) | ✓ | ✓ |
| syd | Sydney, Australia | ✓ | ✓ |
| yyz | Toronto, Canada | ✓ | ✓ |

* **Gateway regions:** "Gateway" regions also have WireGuard gateways, through which you connect to your organization's private network.
* **MPG regions:** "MPG" regions can host [Managed Postgres](/postgres) clusters.

<h2 id="discovering-your-apps-region">
  Discovering your app's region
</h2>

View the list of Fly.io regions with [`fly platform regions`](/flyctl/cmd/fly_platform_regions).

You can see which regions your app is running in with [`fly status`](/flyctl/cmd/fly_status).

[Fly Volumes](/volumes) and [Fly Machines](/machines) are tied to the region they're created in.

Learn more about Machine placement and regional capacity in this [guide](/machines/guides-examples/machine-placement).

When an application instance is started, the three-letter name for the region it's running in is stored in the Machine's `FLY_REGION`  environment variable. This, along with other [Runtime Environment](/machines/runtime-environment) information, is visible to your app running on that instance.

## Related reading

* [Scaling to multiple regions](/blueprints/resilient-apps-multiple-machines#scaling-to-multiple-regions) Read our guide for making your app resilient and globally distributed.
* [Cost Management](/about/cost-management) Find out how region choice, scaling strategy, and app architecture affect your Fly.io bill.
* [Machine Placement and Regional Capacity](/machines/guides-examples/machine-placement) Learn more about how the choice of region (and region capacity) affects where your Machines land.
* [Dynamic Request Routing with fly‑replay](/networking/dynamic-request-routing) Check out our guide to routing requests to specific regions or fallbacks using region codes.
