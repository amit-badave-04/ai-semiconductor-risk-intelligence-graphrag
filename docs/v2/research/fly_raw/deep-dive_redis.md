> ## Documentation Index
> Fetch the complete documentation index at: https://docs.fly.io/llms.txt
> Use this file to discover all available pages before exploring further.

# Upstash Redis

<img src="https://mintcdn.com/fly-io/izaS1l2UMuz6sm1H/images/redis-upstash.png?fit=max&auto=format&n=izaS1l2UMuz6sm1H&q=85&s=6b809776ced663128e2c20a9eb93fa8e" alt="Illustration by Annie Ruygt of a puzzle box figure holding two slices of cake" width="1200" height="690" data-path="images/redis-upstash.png" />

[Upstash Redis](/upstash/redis) is used by the deep dive demo app for its [pubsub](https://redis.io/docs/latest/commands/?group=pubsub) capabilities, but it can do [so much more](https://upstash.com/docs/redis/overall/rediscompatibility).

In particular, Redis is useful for caching:

* [Node and Redis](https://redis.io/learn/develop/node/nodecrashcourse/caching)
* [Rails and Redis](https://guides.rubyonrails.org/caching_with_rails.html#activesupport-cache-rediscachestore)

In the deep dive demo app, updates are broadcast to all Machines via Redis, and then each Machine informs browser clients
of the update via [WebSockets](https://developer.mozilla.org/en-US/docs/Web/API/WebSockets_API). The client requests
updated information from the application using HTTP GET.
