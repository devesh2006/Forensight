from datetime import datetime

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app import copilot
from app.ingest import Base, CaseRecord, Evidence
from app.main import AskIn, active_case, case_anomalies, case_copilot, case_communications, case_correlations, case_evidence, case_locations, case_search, case_timeline, evidence_list, graph, map_analysis, search, timeline


def test_case_scoped_search_timeline_graph_geo_and_copilot():
    engine=create_engine("sqlite://");Base.metadata.create_all(engine);session=sessionmaker(bind=engine)()
    try:
        session.add_all([CaseRecord(case_id="CASE-A",name="A",active=False),CaseRecord(case_id="CASE-B",name="B",active=True)])
        session.add_all([
            Evidence(evidence_id="A-MSG",case_id="CASE-A",type="message",ts=datetime(2026,10,14,20,1),actor="A-ONLY",target="A-CONTACT",summary="CASE A SECRET phrase",source_file="a.csv",source_artifact="csv",meta="{}",sha256="a",source_file_sha256="a",timezone="Asia/Kolkata"),
            Evidence(evidence_id="A-GPS",case_id="CASE-A",type="location",ts=datetime(2026,10,14,20,2),actor="A-ONLY",lat=28.7,lon=77.1,summary="GPS",source_file="a.csv",meta="{}",sha256="b",source_file_sha256="a",timezone="Asia/Kolkata"),
            Evidence(evidence_id="B-MSG",case_id="CASE-B",type="message",ts=datetime(2026,10,14,20,3),actor="B-ONLY",target="B-CONTACT",summary="CASE B evidence",source_file="b.csv",source_artifact="csv",meta="{}",sha256="c",source_file_sha256="b",timezone="Asia/Kolkata"),
        ]);session.commit()
        assert active_case(session)=="CASE-B"
        assert evidence_list(q="CASE A SECRET",type=None,offset=0,limit=100,s=session)["count"]==0
        assert search(q="CASE A SECRET",type=None,offset=0,limit=100,source_file=None,device_id=None,start=None,end=None,relevance_min=None,s=session)["count"]==0
        tl=timeline(start=None,end=None,types=None,entity=None,incident_window_min=None,offset=0,limit=100,s=session)
        assert [x["id"] for x in tl["events"]]==["B-MSG"]
        g=graph(session)
        assert g["edges"] and all("A-ONLY" not in {e["source"],e["target"]} for e in g["edges"])
        geo=map_analysis(session)
        assert geo["count"]==0 and geo["routes"]=={}
        answer=copilot.ask(session,"CASE-B","What messages did A-ONLY send?")
        assert all(e["id"]!="A-MSG" for e in answer["evidence"])
        assert [x["id"] for x in case_evidence("CASE-B",q="CASE A SECRET",type=None,offset=0,limit=100,s=session)["results"]]==[]
        assert [x["id"] for x in case_timeline("CASE-B",start=None,end=None,types=None,entity=None,offset=0,limit=100,s=session)["events"]]==["B-MSG"]
        assert all("A-ONLY" not in {x["source"],x["target"]} for x in case_communications("CASE-B",session)["edges"])
        assert case_locations("CASE-B",offset=0,limit=500,s=session)["count"]==0
        assert case_search("CASE-B",q="CASE A SECRET",type=None,offset=0,limit=100,s=session)["count"]==0
        assert case_correlations("CASE-B",window_min=5,radius_m=400,s=session)==[]
        assert case_anomalies("CASE-B",session)==[]
        answer=case_copilot("CASE-B",AskIn(query="What messages did A-ONLY send?"),session)
        assert all(e["id"]!="A-MSG" for e in answer["evidence"])
    finally:
        session.close();engine.dispose()
