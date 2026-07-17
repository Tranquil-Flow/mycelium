"""Public composition facade for the standalone Mycelium Router."""

from mycelium_router.entry import EntryCoordinator
from mycelium_router.relay import RelayEngine
from mycelium_router.routing import ProgressivePathBuilder, RoutePolicy
from mycelium_router.scheduler import HopScheduler
from mycelium_router.scoring import RouteScorer


class Router:
   def __init__(
      self,
      *,
      node_id,
      topology,
      device_states,
      capacity,
      runtime,
      transport,
      clock,
      id_source,
      config,
   ):
      scorer = RouteScorer(config)
      policy = RoutePolicy(scorer)
      builder = ProgressivePathBuilder(
         policy=policy,
         capacity=capacity,
         id_source=id_source,
      )
      scheduler = HopScheduler(config)
      relay = RelayEngine(
         node_id=node_id,
         runtime=runtime,
         transport=transport,
         scheduler=scheduler,
         clock=clock,
         builder=builder,
         device_states=device_states,
      )
      self.entry = EntryCoordinator(
         node_id=node_id,
         topology=topology,
         device_states=device_states,
         capacity=capacity,
         runtime=runtime,
         transport=transport,
         relay=relay,
         builder=builder,
         clock=clock,
         config=config,
      )
      self.relay = relay

   def admit(self, request, client_sink, **kwargs):
      return self.entry.admit(request, client_sink, **kwargs)

   def start_distributed_prefill(self, request, client_sink, **kwargs):
      return self.entry.start_distributed_prefill(
         request,
         client_sink,
         **kwargs,
      )

   def request_status(self, request_id):
      return self.entry.request_status(request_id)

   def receive_manifest_locked(self, locked):
      return self.entry.receive_manifest_locked(locked)

   def get_request(self, request_id):
      return self.entry.get_request(request_id)

   def generate(self, request_id, *, token_count):
      return self.entry.generate(request_id, token_count=token_count)

   def decode_one(self, request_id):
      return self.entry.decode_one(request_id)

   def decode_one_distributed(self, request_id):
      return self.entry.decode_one_distributed(request_id)

   def cancel(self, request_id):
      return self.entry.cancel(request_id)

   def register_path(self, request, manifest, graph):
      return self.relay.register_path(request, manifest, graph)

   def receive_hop(self, header, payload):
      return self.relay.receive_hop(header, payload)

   def enqueue_hop(self, header, payload):
      return self.relay.enqueue_hop(header, payload)

   def drain_ready_batches(self, **kwargs):
      return self.relay.drain_ready_batches(**kwargs)

   def update_batch_network_stats(self, placement_id, stats):
      return self.relay.update_batch_network_stats(placement_id, stats)

   def batch_decisions(self):
      return self.relay.batch_decisions()

   def batch_execution_profiles(self):
      return self.relay.batch_execution_profiles()

   def batch_network_stats(self):
      return self.relay.batch_network_stats()

   def pending_batch_hops(self):
      return self.relay.pending_batch_hops()

   def next_batch_deadline(self):
      return self.relay.next_batch_deadline()

   def receive_progressive_prefill(self, header, context):
      return self.relay.receive_progressive_prefill(header, context)

   def receive_token_event(self, event):
      return self.entry.receive_token_event(event)

   def receive_prefill_chunk_completed(self, event):
      return self.entry.receive_prefill_chunk_completed(event)

   def receive_failure_report(self, report):
      return self.entry.receive_failure_report(report)
