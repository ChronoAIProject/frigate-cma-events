from copy import deepcopy
from contextlib import closing
from dataclasses import asdict, replace
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch
import uuid

from fastapi.testclient import TestClient

from aggregation import Aggregator, Rules, replay_events, wrap_review
from config import Config
from deploy.render import render
from evidence import EvidenceOptions, FrigateEvidence, select_diverse_frames
from pipeline import Pipeline, matches, validate_predicate
from service import State, cma_event_id, create_app, digest


ROOT = Path(__file__).resolve().parents[1]


def policy():
    return json.loads((ROOT / "examples/ha-state/pipeline.example.json").read_text())


def point(identity="one", at=1000, subject="binary_sensor.synthetic", state="on"):
    return {"specversion":"1.0", "source":policy()["source"]["source"], "id":identity,
            "type":"homeassistant.state_changed", "subject":subject, "time":at,
            "data":{"entity_id":subject,"new_state":{"state":state}}}


class PipelineTests(unittest.TestCase):
    def test_non_dining_example_replays_to_one_metadata_candidate(self):
        pipeline = Pipeline.parse(policy())
        events = json.loads((ROOT / "examples/ha-state/events.example.json").read_text())
        output = replay_events(events, pipeline)
        self.assertEqual(len(output), 1)
        candidate = output[0]
        self.assertEqual(candidate["event_count"], 3)
        self.assertEqual(candidate["decision"], "candidate")
        self.assertEqual(candidate["schema"], "home-activity-candidate.v1")
        self.assertNotIn("meal_id", candidate)
        self.assertNotIn("tableware_seen", candidate)
        self.assertEqual(candidate["evidence_budget"]["max_images"], 0)
        self.assertTrue(any("continuity" in c for c in candidate["cautions"]))
        self.assertEqual(candidate["records"][0]["attributes"]["state"], "on")
        self.assertEqual(candidate["records"][0]["source_event_ids"], ["synthetic-one"])

    def test_vehicle_configuration_needs_no_people_or_tableware(self):
        spec=json.loads((ROOT/"examples/driveway/pipeline.example.json").read_text())
        pipeline=Pipeline.parse(spec);engine=Aggregator(":memory:",pipeline)
        row={"id":"synthetic-car","camera":pipeline.subject,"start_time":100,"end_time":130,
             "severity":"detection","data":{"objects":["car"],"zones":["example_gate"]}}
        try:
            engine.ingest(wrap_review({"type":"end","before":row,"after":row},spec["source"]["source"]),130)
            result=engine.flush(250,source_healthy=True)[0]
            self.assertEqual(result["decision"],"candidate")
            self.assertEqual(result["conditions"]["vehicle_presence"]["seconds"],30)
            self.assertNotIn("meal_id",result)
        finally:engine.close()

    def test_metadata_export_is_bounded_while_id_and_counts_cover_all_events(self):
        spec=policy();spec["output"].update(metadata_records=2,max_event_ids=2)
        result=replay_events([point(str(i),1000+i) for i in range(10)],Pipeline.parse(spec))[0]
        self.assertEqual(result["event_count"],10)
        self.assertEqual(len(result["event_ids"]),2)
        self.assertEqual(len(result["records"]),2)
        self.assertTrue(result["metadata_truncated"])
        self.assertEqual(result["records"][0]["source_event_ids"],["0"])
        self.assertEqual(result["records"][-1]["source_event_ids"],["9"])

    def test_partitions_have_independent_windows_cooldowns_and_daily_limits(self):
        spec = policy();spec["limits"]["max_per_day"] = 1
        engine = Aggregator(":memory:", Pipeline.parse(spec))
        try:
            for i in range(3):
                for subject in ("sensor.alpha", "sensor.beta"):
                    engine.ingest(point(subject+str(i), 1000+i*10, subject), 1000+i*10)
            decisions = engine.flush(1100, source_healthy=True)
            self.assertEqual(len(decisions), 2)
            self.assertEqual({d["subject"] for d in decisions}, {"sensor.alpha","sensor.beta"})
            self.assertTrue(all(d["decision"] == "candidate" for d in decisions))
            self.assertEqual(len({d["candidate_id"] for d in decisions}), 2)
        finally:engine.close()

    def test_redelivery_is_not_three_events_and_identity_changes_are_rejected(self):
        engine = Aggregator(":memory:", Pipeline.parse(policy()))
        try:
            event = point()
            self.assertEqual(engine.ingest(event, 1000), "stored")
            for i in range(4):self.assertEqual(engine.ingest(event,1001+i),"duplicate")
            changed=deepcopy(event);changed["time"]=1001
            with self.assertRaises(ValueError):engine.ingest(changed,1002)
            self.assertEqual(engine.flush(1100,source_healthy=True)[0]["decision"],"repeated_activity_insufficient")
            wrong=point("wrong");wrong["source"]="urn:another:producer"
            with self.assertRaises(ValueError):engine.ingest(wrong,1000)
        finally:engine.close()

    def test_span_mapping_needs_explicit_end_and_unions_overlapping_intervals(self):
        spec = policy();spec["source"].update(mode="span",filter={},types=["activity.span"],
            fields={"id":"data.record_id","phase":"data.phase","start":"data.start","end":"data.end","attributes":{"kind":"data.kind"}},
            health={"mode":"heartbeat"})
        spec["activity"]={"name":"presence","minimum_seconds":100,"match":{"field":"kind","op":"eq","value":"present"}}
        engine=Aggregator(":memory:",Pipeline.parse(spec))
        def span(identity,start,end,phase):
            return {**point(identity+phase,1000),"type":"activity.span","data":{"record_id":identity,"phase":phase,"start":start,"end":end,"kind":"present"}}
        try:
            engine.ingest(span("a",100,None,"new"),100)
            self.assertEqual(engine.flush(10000,source_healthy=True),[])
            engine.ingest(span("a",100,300,"end"),300)
            engine.ingest(span("b",200,400,"end"),400)
            result=engine.flush(500,source_healthy=True)[0]
            self.assertEqual(result["activity_seconds"],300)
            self.assertEqual(result["decision"],"candidate")
        finally:engine.close()

    def test_predicates_are_typed_bounded_and_do_not_evaluate_code(self):
        predicate={"all":[{"field":"state","op":"in","value":["on","active"]},
                           {"field":"watts","op":"gte","value":20},
                           {"not":{"field":"missing","op":"exists","value":True}}]}
        validate_predicate(predicate)
        self.assertTrue(matches({"state":"on","watts":25},predicate))
        self.assertFalse(matches({"state":"on","watts":True},predicate))
        self.assertFalse(matches({"state":"on","watts":"25"},predicate))
        self.assertFalse(matches({"state":"off","watts":25},predicate))
        with self.assertRaises(ValueError):validate_predicate({"field":"x","op":"eval","value":"arbitrary()"})
        with self.assertRaises(ValueError):validate_predicate({"field":"__import__('os')","op":"eq","value":"x"})

    def test_configuration_rejects_typos_unbounded_costs_and_unsafe_health_modes(self):
        variants=[]
        for change in (
            lambda d:d.update(command="arbitrary"),
            lambda d:d["window"].update(merge_seonds=1),
            lambda d:d["activity"].update(minimum_events=True),
            lambda d:d["source"].update(mode="span"),
            lambda d:d["evidence"].update(adapter="frigate"),
            lambda d:d["output"].update(aliases={"status":"candidate_id"}),
            lambda d:d["output"].update(budget={"max_images":100}),
            lambda d:d["evidence"].update(options={"novelty_weight":float("nan")}),
            lambda d:d["evidence"].update(options={"sample_frames":12.5}),
            lambda d:d["evidence"].update(options={"crop_padding":"0.04"}),
            lambda d:d["evidence"].update(options={"max_width":True}),
            lambda d:d.update(requirements=[{"name":"repeated_activity"}]),
        ):
            spec=policy();change(spec);variants.append(spec)
        for spec in variants:
            with self.subTest(spec=spec),self.assertRaises(ValueError):Pipeline.parse(spec)

    def test_config_and_renderer_work_without_dining_or_camera_parameters(self):
        env={"PIPELINE_CONFIG_JSON":json.dumps(policy()),"PROVIDER_TOKEN":"p"*48,"API_TOKEN":"a"*48,
             "WORKER_TOKEN":"w"*48,"WORKER_SERVICE":"synthetic-results"}
        with patch.dict("os.environ",env,clear=True):config=Config.from_env()
        self.assertEqual(config.pipeline.spec["source"]["adapter"],"cloudevents")
        self.assertEqual(config.evidence_token,"w"*48)
        spec=render("ghcr.io/example/app:synthetic","api",env)
        service=spec["services"]["api"]
        self.assertNotIn("CAMERA",service["env"])
        self.assertNotIn("WORKER_TOKEN",service["env"])
        self.assertIn("WORKER_TOKEN",service["secret_keys"])
        with tempfile.TemporaryDirectory() as directory:
            file=Path(directory)/"pipeline.json";file.write_text(json.dumps(policy()))
            file_env={k:v for k,v in env.items() if k!="PIPELINE_CONFIG_JSON"};file_env["PIPELINE_CONFIG_FILE"]=str(file)
            with patch.dict("os.environ",file_env,clear=True):loaded=Config.from_env()
            self.assertEqual(loaded.pipeline.spec,config.pipeline.spec)
            self.assertEqual(json.loads(render("image","api",file_env)["services"]["api"]["env"]["PIPELINE_CONFIG_JSON"]),config.pipeline.spec)

    def test_frigate_evidence_labels_and_selection_weights_are_configuration(self):
        client=FrigateEvidence("https://proxy.example/service","synthetic","example-camera","gate",labels=("car",))
        row={"camera":"example-camera","label":"car","zones":["gate"],"start_time":100,"end_time":200,"data":{}}
        with patch.object(client,"get",return_value=json.dumps([row,{**row,"label":"bowl"}])) as get:
            self.assertEqual(len(client.object_events(100,200)),1)
            self.assertIn("labels=car",get.call_args.args[0])
        records=[{"phase":"early","timestamp":100,"tracked_objects":10,"quality":0.6,"_signature":b'\x10'*12},
                 {"phase":"early","timestamp":200,"tracked_objects":1,"quality":0.9,"_signature":b'\x50'*12}]
        by_density=EvidenceOptions(density_weight=1,quality_weight=0,novelty_weight=0)
        by_quality=EvidenceOptions(density_weight=0,quality_weight=1,novelty_weight=0)
        self.assertEqual(select_diverse_frames(deepcopy(records),3,by_density)[0]["timestamp"],100)
        self.assertEqual(select_diverse_frames(deepcopy(records),3,by_quality)[0]["timestamp"],200)

    def test_legacy_state_upgrades_then_accepts_equivalent_explicit_policy(self):
        with tempfile.TemporaryDirectory() as directory:
            rules=Rules(camera="example-camera",seats_zone="example-seats",table_zone="example-table")
            config=Config(rules,Path(directory),"p"*48,"a"*48,"urn:example:source")
            state=State(config);engine=state._relay_engine()
            row={"id":"example-review","camera":rules.camera,"start_time":100,"end_time":1000,"severity":"alert",
                 "data":{"objects":["person","bowl"],"zones":[rules.seats_zone,rules.table_zone]}}
            engine.ingest(wrap_review({"type":"end","before":row,"after":row},config.event_source),1000)
            result=engine.flush(1400,source_healthy=True)[0]
            identity=str(uuid.uuid5(uuid.NAMESPACE_URL,rules.camera+"/example-review"))
            self.assertEqual(result["meal_id"],identity)
            state.close()
            # Simulate a pre-upgrade database: it knows only the original rules digest.
            import sqlite3
            with closing(sqlite3.connect(Path(directory)/"relay-ingest.sqlite3")) as db, db:
                db.execute("DELETE FROM meta WHERE key='policy_digest'")
            explicit=replace(config,rules=config.pipeline)
            with self.assertRaises(ValueError):State(explicit)
            upgraded=State(config)
            self.assertEqual(upgraded.candidate(identity)["meal_id"],identity)
            self.assertEqual(upgraded.get_meta(upgraded._relay_engine(),"policy_digest"),digest(config.pipeline.state_document()))
            upgraded.close()
            migrated=State(explicit)
            self.assertEqual(migrated.candidate(identity)["candidate_id"],identity)
            self.assertEqual(migrated._relay_engine().db.execute("SELECT count(*) FROM decisions").fetchone()[0],1)
            self.assertEqual(len(migrated._relay_engine().db.execute("PRAGMA table_info(decisions)").fetchall()),5)
            migrated.close()
            changed=deepcopy(config.pipeline.spec);changed["activity"]["minimum_seconds"]+=1
            with self.assertRaises(ValueError):State(replace(config,rules=Pipeline.parse(changed)))
            media_only=deepcopy(config.pipeline.spec);media_only["evidence"]["options"]["sample_frames"]=30
            compatible=State(replace(config,rules=Pipeline.parse(media_only)));compatible.close()

    def test_generic_http_ingest_delivery_callback_and_offline_replay(self):
        with tempfile.TemporaryDirectory() as directory:
            env={"PIPELINE_CONFIG_JSON":json.dumps(policy()),"DATA_DIR":directory,"PROVIDER_TOKEN":"p"*48,
                 "API_TOKEN":"a"*48,"WORKER_TOKEN":"w"*48,"WORKER_SERVICE":"synthetic-results",
                 "CMA_EVENT_URL":"https://cma.example/events","CMA_HMAC_KEY_ID":"synthetic","CMA_HMAC_SECRET":"s"*48}
            with patch.dict("os.environ",env,clear=True):config=Config.from_env()
            with TestClient(create_app(config)) as client:
                now=time.time()
                events=[point(str(i),now-120+i*10) for i in range(3)]
                relay={"Authorization":"Bearer "+config.relay_token};operator={"Authorization":"Bearer "+config.api_token}
                with patch("service.urlopen") as send:
                    replay=client.post("/api/replay",headers=operator,json={"events":events})
                    self.assertEqual(replay.status_code,200,replay.text)
                    self.assertEqual(len(replay.json()["decisions"]),1)
                    self.assertEqual(client.get("/api/status",headers=operator).json()["bindings"],0)
                    send.assert_not_called()
                for event in events:
                    self.assertEqual(client.post("/ingest",headers=relay,json=event).status_code,200)
                client.post("/ingest",headers=relay,json=events[0])
                self.assertEqual(client.app.state.worker.flush(),0)
                engine=client.app.state.worker._relay_engine()
                with engine.db:engine.db.execute("UPDATE reviews SET received=?",(now-20,))
                class Response:
                    status=202
                    def __enter__(self):return self
                    def __exit__(self,*_):return False
                    def read(self,*_):return b"accepted"
                with patch("service.urlopen",return_value=Response()) as send:
                    self.assertEqual(client.app.state.worker.flush(),1)
                    self.assertEqual(send.call_count,1)
                    delivered=json.loads(send.call_args.args[0].data)
                self.assertEqual(delivered["type"],"home.activity_candidate")
                candidate=delivered["data"];identity=candidate["candidate_id"]
                self.assertNotIn("evidence",candidate)
                self.assertNotIn("meal_id",candidate)
                self.assertEqual(candidate["event_count"],3)
                self.assertEqual(delivered["id"],cma_event_id(config.event_source,identity,"home.activity_candidate"))
                worker={"Authorization":"Bearer "+config.evidence_token}
                self.assertEqual(client.get("/evidence/"+identity,headers=worker).status_code,503)
                result={"status":"processed","reference":"synthetic-record","uncertainty":"Received events only"}
                self.assertEqual(client.post("/results/"+identity,headers=operator,json=result).status_code,401)
                self.assertEqual(client.post("/results/"+identity,headers=worker,json=result).status_code,200)
                self.assertEqual(client.get("/api/results/"+identity,headers=operator).json()["result"],result)
                self.assertEqual(client.post("/results/"+identity,headers=worker,json={"status":"recorded","sheet_range":"x"}).status_code,400)
                self.assertEqual(client.get("/api/pipeline",headers=operator).status_code,200)
                self.assertEqual(client.get("/api/pipeline").status_code,401)
