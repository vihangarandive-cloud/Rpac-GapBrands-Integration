import uuid
import asyncio
from datetime import datetime
from typing import List
from fastapi import FastAPI, Request, BackgroundTasks, HTTPException
from fastapi.responses import HTMLResponse, Response, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sse_starlette.sse import EventSourceResponse
from pydantic import BaseModel
import starlette.concurrency

from portal_api import PortalAPI
from auth import get_authenticated_session
app = FastAPI(title="GAP Brands Portal Automation")

# Setup templates
templates = Jinja2Templates(directory="templates")

# Mount static files — serve from wwwroot/static (where logo and assets live)
app.mount("/static", StaticFiles(directory="wwwroot/static"), name="static")

# In-memory store for job progress and results
# Structure: { job_id: {"queue": asyncio.Queue, "zip_bytes": bytes, "summary": dict, "done": bool} }
jobs_store = {}

class JobRequest(BaseModel):
    po_numbers: str
    brand: str = "Old Navy"
    product_gender: str = ""
    product_type: str = ""
    vendor_po: str = ""
    franchise: str = "None"
    order_lpn_labels: str = ""
    price_tickets: str = "None"
    price_ticket_code: str = "None"
    season: str = ""
    order_type: str = "MAS"

def run_job_sync(job_id: str, po_list: List[str]):
    """Synchronous function that runs the portal API process."""
    api = PortalAPI()
    
    # We need a way to pass messages to the async queue.
    # Since this runs in a thread pool, we must use asyncio.run_coroutine_threadsafe
    # but to keep it simple without passing event loops, we can just store the generator
    # and iterate over it in an async wrapper.
    return api.process_job(po_list)

async def background_job_runner(job_id: str, po_list: List[str], req: JobRequest):
    """Async background task that iterates the sync generator and updates the queue."""
    queue = jobs_store[job_id]["queue"]
    
    try:
        api = PortalAPI()
        # The generator yields strings, and returns the final tuple when exhausted.
        # We can iterate over the generator in a thread to avoid blocking the event loop.
        
        # We need a custom wrapper to yield from a sync generator in an async context
        def get_next_item(gen):
            try:
                return next(gen), False
            except StopIteration as e:
                return e.value, True
                
        gen = api.process_job(po_list, req)
        
        while True:
            val, is_done = await starlette.concurrency.run_in_threadpool(get_next_item, gen)
            
            if is_done:
                # val is the returned tuple: (zip_bytes, successful_pos, missing_pos)
                zip_bytes, successful, missing = val if val else (None, [], [])
                jobs_store[job_id]["zip_bytes"] = zip_bytes
                jobs_store[job_id]["summary"] = {
                    "successful": successful,
                    "missing": missing
                }
                jobs_store[job_id]["done"] = True
                await queue.put("DONE")
                break
            else:
                # val is a progress string
                await queue.put(val)
                
    except Exception as e:
        await queue.put(f"ERROR: Job failed unexpectedly - {str(e)}")
        jobs_store[job_id]["done"] = True
        await queue.put("DONE")

@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    return templates.TemplateResponse(request=request, name="index.html")

@app.post("/api/start-job")
async def start_job(request: JobRequest, background_tasks: BackgroundTasks):
    po_text = request.po_numbers
    # Split by comma or newline, clean whitespace
    po_list = []
    for line in po_text.replace(",", "\n").split("\n"):
        clean_po = line.strip()
        if clean_po:
            po_list.append(clean_po)
            
    if not po_list:
        raise HTTPException(status_code=400, detail="No PO numbers provided")
        
    job_id = str(uuid.uuid4())
    jobs_store[job_id] = {
        "queue": asyncio.Queue(),
        "zip_bytes": None,
        "summary": None,
        "done": False
    }
    
    background_tasks.add_task(background_job_runner, job_id, po_list, request)
    return {"job_id": job_id}

@app.get("/api/job-progress/{job_id}")
async def progress(job_id: str, request: Request):
    if job_id not in jobs_store:
        raise HTTPException(status_code=404, detail="Job not found")
        
    queue = jobs_store[job_id]["queue"]
    
    async def event_generator():
        while True:
            if await request.is_disconnected():
                break
                
            try:
                # Wait for a message from the queue
                message = await asyncio.wait_for(queue.get(), timeout=15.0)
                yield {"data": message}
                if message == "DONE":
                    break
            except asyncio.TimeoutError:
                # Send a keep-alive comment
                yield {"comment": "keep-alive"}
                
    return EventSourceResponse(event_generator())

@app.get("/api/download/{job_id}")
async def download_zip(job_id: str):
    if job_id not in jobs_store:
        raise HTTPException(status_code=404, detail="Job not found")
        
    job_data = jobs_store[job_id]
    if not job_data["done"]:
        raise HTTPException(status_code=400, detail="Job is not complete yet")
        
    zip_bytes = job_data["zip_bytes"]
    if not zip_bytes:
        raise HTTPException(status_code=500, detail="No zip file generated (possibly all POs failed)")
        
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = f"GAP_Orders_{timestamp}.zip"
    
    return Response(
        content=zip_bytes,
        media_type="application/zip",
        headers={
            "Content-Disposition": f"attachment; filename={filename}"
        }
    )

@app.get("/api/summary/{job_id}")
async def get_summary(job_id: str):
    if job_id not in jobs_store:
        raise HTTPException(status_code=404, detail="Job not found")
        
    job_data = jobs_store[job_id]
    if not job_data["done"]:
        raise HTTPException(status_code=400, detail="Job is not complete yet")
        
    return job_data["summary"]

# --- Proxy Endpoints for Dynamic Dropdowns ---

def _fetch_from_gap(url: str):
    try:
        session = get_authenticated_session()
        res = session.get(url)
        if res.ok:
            return res.json()
        return {"error": f"Failed with status {res.status_code}", "details": res.text}
    except Exception as e:
        return {"error": str(e)}

@app.get("/api/gap-brands/genders")
async def get_genders():
    # GapBrands endpoint for Product Genders
    return JSONResponse(_fetch_from_gap("https://gapbrands.r-pac.com.hk/api/app/product-gender"))

@app.get("/api/gap-brands/types")
async def get_types(gender: str = "", brand: str = ""):
    import urllib.parse
    
    # Use the correct GapBrands endpoint that filters by Brand and Gender
    url = "https://gapbrands.r-pac.com.hk/api/app/size-chart/product-type-by-product-gender"
    
    params = []
    if gender:
        params.append(f"productGender={urllib.parse.quote(gender)}")
    
    if params:
        url += "?" + "&".join(params)
        
    data = _fetch_from_gap(url)
    return JSONResponse(data)



@app.get("/api/gap-brands/seasons")
async def get_seasons():
    # GapBrands endpoint for Seasons
    return JSONResponse(_fetch_from_gap("https://gapbrands.r-pac.com.hk/api/app/season"))

@app.get("/api/gap-brands/tickets")
async def get_tickets(brand: str = ""):
    # GapBrands API does not support filtering Price Tickets by Gender/Type
    # and `item-code-list` returns 2700+ tickets.
    # Therefore, we fallback to the known, curated lists for Old Navy and GAP.
    tickets = {
        "Old Navy": [
            { "text": "None", "value": "None" },
            { "text": "100573", "value": "3a1b4887-fe03-7e7f-6654-1979283a1174" },
            { "text": "1017592", "value": "3a19f937-f003-fa59-1a24-2783f0bb8161" },
            { "text": "1017593", "value": "3a16e65a-49f8-4470-29d1-3392c674367a" },
            { "text": "101885", "value": "3a1b4884-ae99-bdf6-3f3e-be57c5a6e637" },
            { "text": "1035872", "value": "3a16e653-ac5f-34ba-7e81-bd6b78078898" },
            { "text": "1047431", "value": "3a137ad6-da27-edca-fba8-74b4cef0ac90" },
            { "text": "1047433", "value": "3a138aca-0e40-d3c4-622f-f241e4eb972a" },
            { "text": "1047792", "value": "3a17097c-9a55-5457-cf4d-0547513826cc" },
            { "text": "1048004", "value": "3a170983-9d06-30a5-109b-cb21b0555c4d" },
            { "text": "1048772", "value": "3a145910-fe9d-8566-c025-c4f99c66caf8" },
            { "text": "1048773", "value": "3a145914-83c0-8110-3846-07e91c983379" },
            { "text": "1048996", "value": "3a175ccc-43fe-e699-0b12-231aed4e45cd" },
            { "text": "1048997", "value": "3a175cc9-bd6b-73bb-a7ed-f1b2848f7096" },
            { "text": "1062280", "value": "3a1df56a-dfed-0af1-7d06-d1482c61520e" },
            { "text": "1065611", "value": "3a1f39a2-3f6f-04a7-cfbd-caad8ae4a347" },
            { "text": "1065612", "value": "3a1f39ab-5be7-2296-1108-d70b541868fc" },
            { "text": "112044", "value": "3a1b4886-6224-df2f-9ca1-961f322d3a2e" },
            { "text": "167356", "value": "3a19f9a6-b25a-cd9b-8ce4-de9ed28d1704" },
            { "text": "269032", "value": "3a19eb6f-ed6a-c527-5caa-8aac1c47d29e" },
            { "text": "269034", "value": "3a19f927-288b-1ce6-0c7b-bbcbe87740d5" }
        ],
        "GAP": [
            { "text": "None", "value": "None" },
            { "text": "158105", "value": "3a2142a3-83c7-4048-53b7-3386a230c16a" },
            { "text": "BGT-1", "value": "3a19b806-46db-65b3-b8ea-61c127fff720" },
            { "text": "BGT-19", "value": "3a19b7c4-d9e4-c944-cb1b-38c1808a2ee5" },
            { "text": "BPT1", "value": "3a1973ad-e89f-a56b-3bb1-d7e1f74f8265" },
            { "text": "BPT2", "value": "3a1a99be-a202-4b84-5937-bc0bd23f2c47" },
            { "text": "GB-812", "value": "3a1c1c15-245d-4131-4188-c0b00c1e34b4" },
            { "text": "GBBP", "value": "3a1e7ac4-f58b-84e7-5a66-ca7bd806cfd0" },
            { "text": "GBLUE_1", "value": "3a1da454-ee17-6bd6-f333-2389095ea8b3" },
            { "text": "GBSL", "value": "3a1e7224-bf01-0e98-7967-79ca444a1290" },
            { "text": "GFOL-11", "value": "3a1a371a-1efe-df4f-762b-ef97c12cc61f" },
            { "text": "GFOL-8", "value": "3a19b80a-a8b4-cb93-4e54-b3e9a9e56840" },
            { "text": "GFOLB-11", "value": "3a1ab2ce-e64d-d044-9bed-d028fcb0e082" },
            { "text": "GFOLB-14", "value": "3a19b80c-9e5d-9de0-c70a-8b150b67798f" },
            { "text": "GFOLK-10", "value": "3a19b80f-d9da-e184-49cf-66f8172d733b" },
            { "text": "GFOLK-11", "value": "3a1a99bb-bceb-bcab-0a69-fa7e7a388112" },
            { "text": "GKT-1", "value": "3a19b807-847a-d74f-eef8-975562ea9a26" },
            { "text": "GKT-3", "value": "3a19b7c6-ff8a-8f2d-5df2-2b24c714a45a" },
            { "text": "GOB1", "value": "3a1973b6-fcbb-7c75-cbc6-9c5af3f37106" },
            { "text": "GOB2", "value": "3a1973c0-a354-31fd-8896-9b5a76d53c59" },
            { "text": "GOB3", "value": "3a2097c7-eea1-4f9e-8f43-cfbfbfd6dc17" },
            { "text": "GOB4", "value": "3a2097cc-95b2-6ed5-1b98-3aafea4b9941" },
            { "text": "GOBPT-11", "value": "3a1ab2d1-cbab-745b-fd5c-aea99606dddd" },
            { "text": "GOBPT-14", "value": "3a19b80b-abd1-d2ea-1f88-ecf997af861b" },
            { "text": "GOKPT-10", "value": "3a19b80e-e23c-16ba-eab5-bda8f9b6aef6" },
            { "text": "GOKPT-11", "value": "3a1a6640-652b-9915-0028-4b43cda47c03" },
            { "text": "GOPT-8", "value": "3a19b809-3e15-8a54-8557-8a799211cf87" },
            { "text": "GOYPT-11", "value": "3a1a99b7-8114-b88d-44ac-fefab6a6c8d6" },
            { "text": "GPT-1", "value": "3a16b238-14ec-1c28-0e8a-056502f358c1" },
            { "text": "GPT-26", "value": "3a1a3717-011e-ffe7-9f87-d371d0fe8122" },
            { "text": "GSKU-10-U", "value": "3a1882a3-1a71-92e6-5b0b-8cdb9f4aa3e9" }
        ]
    }
    return JSONResponse(tickets.get(brand, tickets["Old Navy"]))

@app.get("/api/gap-brands/franchises")
async def get_franchises():
    return JSONResponse(_fetch_from_gap("https://gapbrands.r-pac.com.hk/api/app/templates/market-code-list"))
