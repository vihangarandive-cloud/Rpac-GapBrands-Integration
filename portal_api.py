import io
import time
import zipfile
import urllib.parse
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import List, Dict, Generator, Any, Tuple
import requests
import json
import re

from auth import get_authenticated_session

def to_form_urlencoded(cart_items, token):
    data = {
        "confirmDto[userShipBillId]": "3a0ec547-277d-bfa1-b124-e5a138679563",
        "confirmDto[printShopId]": "3a0cc21c-7ffe-1270-cb6a-7649de5278d0",
        "confirmDto[deliveryMethod]": "0",
        "confirmDto[courier]": "no",
        "confirmDto[account]": "",
        "confirmDto[specialRequest]": "",
        "confirmDto[customerPo]": "",
        "confirmDto[vendorPo]": "",
    }
    
    for i, item in enumerate(cart_items):
        prefix = f"confirmDto[cartItemDtos][{i}]"
        
        for k, v in item.items():
            if k not in ["cartCatalogItemDto", "cartPoItemDto", "cartGapPoItemDto", "cartOldNavyPoItemDto"]:
                if v is not None and v != "":
                    data[f"{prefix}[{k}]"] = str(v)
                
        if "cartGapPoItemDto" in item and item["cartGapPoItemDto"]:
            gap_dto = item["cartGapPoItemDto"]
            for k, v in gap_dto.items():
                if isinstance(v, dict) or isinstance(v, list):
                    pass
                else:
                    if v is not None and v != "":
                        data[f"{prefix}[cartGapPoItemDto][{k}]"] = str(v)
                    
        if "cartOldNavyPoItemDto" in item and item["cartOldNavyPoItemDto"]:
            on_dto = item["cartOldNavyPoItemDto"]
            for k, v in on_dto.items():
                if isinstance(v, dict) or isinstance(v, list):
                    pass
                else:
                    if v is not None and v != "":
                        data[f"{prefix}[cartOldNavyPoItemDto][{k}]"] = str(v)
                    
    data["__RequestVerificationToken"] = token
    return urllib.parse.urlencode(data)

def calculate_lpn_quantity(base_qty: int, order_type: str, brand: str = "") -> int:
    """
    Calculate final total ordered LPN quantity according to slab rules.
    - For Old Navy (US/CA 3-up print layout): the quantity must always be an odd number.
    - For other brands (e.g. GAP): the quantity must always be an even number.
    """
    if base_qty <= 0:
        return 0

    additional_lpn = 0
    if order_type == "MAS":
        if 1 <= base_qty <= 100:
            additional_lpn = 12
        elif 100 < base_qty <= 500:
            additional_lpn = 18
        elif 500 < base_qty <= 1000:
            additional_lpn = 20
        elif 1000 < base_qty <= 5000:
            additional_lpn = 24
        elif base_qty > 5000:
            additional_lpn = 30
    else:
        additional_lpn = 10

    total_qty = base_qty + additional_lpn

    is_old_navy = bool(brand and "old" in brand.lower() and "navy" in brand.lower())
    if is_old_navy:
        # ONPT Reference Stickers - US/CA (3-Up): must be an odd number
        if total_qty % 2 == 0:
            total_qty += 1
    else:
        # Other brands: must be an even number
        if total_qty % 2 != 0:
            total_qty += 1

    return total_qty

def fetch_single_po(session, po_num: str, req_brand: str, product_gender: str, product_type: str, franchise: str, vendor_po: str, season: str, price_tickets: str, price_ticket_code: str, order_lpn_labels: str, order_type: str, token: str):
    import time
    po_num = po_num.strip()
    if not po_num:
        return None, False, "Empty PO number"
    
    search_url = f"https://gapbrands.r-pac.com.hk/api/app/gap-po-item-head?poNo={po_num}&Sorting=CreationTime%20desc&SkipCount=0&MaxResultCount=1"
        
    try:
        search_res = session.get(search_url)
        if not search_res.ok:
            return po_num, False, f"Search failed (status code {search_res.status_code})"
        
        items = search_res.json().get('items', [])
        if not items:
            return po_num, False, "PO not found"
            
        item = items[0]
        gapPoItemHeadId = item['id']
        
        detail_url = f"https://gapbrands.r-pac.com.hk/api/app/gap-po-item?itemHeadId={gapPoItemHeadId}"
        detail_res = session.get(detail_url)
        if not detail_res.ok:
            return po_num, False, f"Details failed (status code {detail_res.status_code})"
            
        detail_items = detail_res.json().get("items", [])
        if not detail_items:
            return po_num, False, "No detail items found"
            
        details = []
        qty_dict = {}
        for i, d_item in enumerate(detail_items):
            base_qty = int(d_item["quantity"])
            det = {
                "Id": d_item["id"],
                "quantity": str(base_qty),
                "poNo": po_num
            }
            if order_lpn_labels == "YES":
                lpn_qty = str(calculate_lpn_quantity(base_qty, order_type, req_brand))
                det["lpnQuantity"] = lpn_qty
                det["quantity"] = lpn_qty
                qty_dict[f"lpnQty_{d_item['id']}"] = lpn_qty
                qty_dict[f"qty_{d_item['id']}"] = lpn_qty
                
            details.append(det)
            if order_lpn_labels != "YES":
                qty_dict[f"qty_{d_item['id']}"] = str(base_qty)
            

            
        if price_tickets == "None" or not price_tickets:
            json_data_str = "[]"
            item_codes = ""
            item_types = ""
            t_type = ""
        else:
            json_data_str = f'[{{"itemCode":"{price_ticket_code}","id":"{price_tickets}","itemType":"PriceTicket","reprint":"False"}}]'
            item_codes = price_ticket_code
            item_types = "PriceTicket"
            if req_brand == "Old Navy":
                t_type = "ONPT"
            else:
                t_type = price_ticket_code

        payload = {
            "type": "0",
            "style": detail_items[0].get("style", ""),
            "brand": "OldNavy" if req_brand == "Old Navy" else "Gap",
            "ticketTypes": t_type,
            "itemHeadIds": gapPoItemHeadId,
            "poNos": "",
            "messagePoNos": po_num,
            "productGenderId": product_gender,
            "productTypeId": product_type,
            "vendorPo": vendor_po if vendor_po else "no",
            "overage": "0",
            "otherOverage": "",
            "needLpnStickes": "1" if order_lpn_labels == "YES" else "0",
            "lpnOverage": "0",
            "otherLpnOverage": "",
            "priceTicketId": price_tickets if price_tickets != "None" else "",
            "coating": "0",
            "needAdditionalItems": "0",
            "matchBookId": "",
            "mainHangtagId": "",
            "secondaryHangtagId": "",
            "mainLabelId": "",
            "sizeLabelId": "",
            "packagingId": "",
            "traceabilityItemsId": "",
            "fitDisplay": "",
            "fabricationDisplay": "",
            "productNameText": "",
            "productNameId": "",
            "pieceSet": "NONE",
            "cooId": "d2c28254-45b4-4b55-8677-6c82668f7a46" if req_brand != "Old Navy" else "",
            "seasonId": "",
            "seasonValue": season,
            "sustainableSymbols": "",
            "styleNumber": "",
            "colorCode": "",
            "manufacturingDate": "",
            "productType": "",
            "styleText": "",
            "qrCode": "",
            "attribute1Id": "",
            "winkCopy": "",
            "attribute2Id": "",
            "attribute3Id": "",
            "bottomAttributesInseamId": "",
            "bottomAttributesRiseId": "",
            "bottomAttributesLengthId": "",
            "qualityAttributesId": "",
            "gapAttributes1Id": "",
            "gapAttributes2Id": "",
            "gapAttributes3Id": "",
            "gapAttributes4Id": "",
            "gapAttributes5Id": "",
            "gapAttributes6Id": "",
            "gapAttributes7Id": "",
            "gapAttributes8Id": "",
            "crockinInfo": "0",
            "crockinRecyclePercent": "0",
            "crockinRecyclePercentValue": "",
            "kcCode": "",
            "copyrightYearId": "",
            "antiMigrations": "0",
            "babyStripInId": "",
            "warmthLevel": "",
            "sleeveLengthId": "",
            "importFabricOrYarn": "0",
            "baseItem": {"compositions_Percentages": "0"},
            "details": details,
            "ItemCodes": item_codes,
            "ItemTypes": item_types,
            "LpnItemCodes": "",
            "LpnItemTypes": "",
            "TicketTypes": t_type,
            "hasReprint": "0",
            "marketCode": "1",
            "fabricationId": "",
            "fitId": "",
            "gapGarmentPartId": "",
            "fiberId": "",
            "exclusiveCalloutId": "",
            "babyStripIn": "",
            "SleeveLengthId": "",
            "jsonData": json_data_str,
            "careLabelData": "",
            "__RequestVerificationToken": token
        }
        
        if franchise != "None":
            payload["franchisesId"] = franchise
            
        payload.update(qty_dict)
        
        import json
        to_job_url = "https://gapbrands.r-pac.com.hk/api/app/gap-po-item/to-job?type=0"
            
        to_job_res = session.post(to_job_url, json=payload, headers={"X-XSRF-TOKEN": token})
        
        if not to_job_res.ok:
            return po_num, False, f"to-job failed: {to_job_res.status_code}"
            
        res_data = to_job_res.json()
        if res_data.get("status") is True:
            return po_num, True, None
        else:
            return po_num, False, f"to-job error: {res_data.get('message')} | Payload ID: {price_tickets}"
            
    except Exception as e:
        return po_num, False, str(e)

def download_single_job(session, job_id: str, brand: str = "GAP"):
    try:
        url = f"https://gapbrands.r-pac.com.hk/api/app/gap-po-order-detail/{job_id}/export-order-to-file"
            
        res = session.post(url)
        if not res.ok:
            return job_id, False, None, None, f"Export API error (status code {res.status_code})"
        
        content = res.content
        filename = f"Order_{job_id}.xlsx"
        
        try:
            json_resp = res.json()
            if "fileName" in json_resp and json_resp["fileName"]:
                filename = json_resp["fileName"]
            
            if "fileToken" in json_resp:
                token = json_resp["fileToken"]
                ftype = json_resp.get("fileType", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
                fname = json_resp.get("fileName", filename)
                
                # Use the correct ASP.NET Zero file download endpoint
                import urllib.parse
                dl_url = f"https://gapbrands.r-pac.com.hk/File/DownloadTempFile?fileType={urllib.parse.quote(ftype)}&fileToken={urllib.parse.quote(token)}&fileName={urllib.parse.quote(fname)}"
                
                dl_res = session.get(dl_url)
                if dl_res.ok:
                    content = dl_res.content
                    cd = dl_res.headers.get("Content-Disposition")
                    if cd and "filename=" in cd:
                        fname_match = re.search(r'filename="?([^";]+)"?', cd)
                        if fname_match:
                            filename = fname_match.group(1).strip()
        except Exception:
            pass
                
        if len(content) > 100:
            return job_id, True, filename, content, None
        else:
            return job_id, False, None, None, "File content too short or invalid"
    except Exception as e:
        return job_id, False, None, None, str(e)

class PortalAPI:
    def __init__(self):
        self.session = None

    def authenticate(self):
        self.session = get_authenticated_session()
        return self.session is not None

    def process_job(self, po_list: List[str], req=None) -> Generator[str, None, Tuple[bytes, List[str], List[str]]]:
        successful_pos = []
        missing_pos = []
        file_result = None

        try:
            if not self.session:
                yield "Authenticating..."
                if not self.authenticate():
                    yield "ERROR: Authentication failed."
                    return None, [], po_list
                yield "Authentication successful."
                
            try:
                cart_res = self.session.get("https://gapbrands.r-pac.com.hk/api/app/shopping-cart")
                if cart_res.ok:
                    cart_items = cart_res.json().get("items", [])
                    if cart_items:
                        # Fetch token needed for POST
                        html_res = self.session.get("https://gapbrands.r-pac.com.hk/Main/ShoppingCart")
                        token_match = re.search(r'name="__RequestVerificationToken" type="hidden" value="([^"]+)"', html_res.text)
                        del_token = token_match.group(1) if token_match else ""
                        
                        delete_payload = [{"cartId": item["cartId"], "type": item.get("type", 0)} for item in cart_items]
                        self.session.post(
                            "https://gapbrands.r-pac.com.hk/api/app/shopping-cart/bulk-delete",
                            json=delete_payload,
                            headers={"X-XSRF-TOKEN": del_token, "x-requested-with": "XMLHttpRequest"}
                        )
            except Exception:
                pass
                
            token = ""
            try:
                html_res = self.session.get("https://gapbrands.r-pac.com.hk/Main/ShoppingCart")
                token_match = re.search(r'name="__RequestVerificationToken" type="hidden" value="([^"]+)"', html_res.text)
                if token_match:
                    token = token_match.group(1)
            except:
                pass
            
            brand = getattr(req, 'brand', "Old Navy")
            product_gender = getattr(req, 'product_gender', "")
            product_type = getattr(req, 'product_type', "")
            franchise = getattr(req, 'franchise', "None")
            vendor_po = getattr(req, 'vendor_po', "")
            season = getattr(req, 'season', "")
            price_tickets = getattr(req, 'price_tickets', "None")
            price_ticket_code = getattr(req, 'price_ticket_code', "None")
            order_lpn_labels = getattr(req, 'order_lpn_labels', "NO")
            order_type = getattr(req, 'order_type', "MAS")

            clean_po_list = []
            for p in po_list:
                p = p.strip()
                if p and p not in clean_po_list:
                    clean_po_list.append(p)
            if not clean_po_list:
                yield "ERROR: No valid PO numbers provided."
                return None, [], []

            yield f"Searching {len(clean_po_list)} POs in parallel..."

            # Snapshot cart IDs before adding to identify our new items later
            try:
                pre_cart_res = self.session.get("https://gapbrands.r-pac.com.hk/api/app/shopping-cart")
                pre_cart_ids = set(item["cartId"] for item in pre_cart_res.json().get("items", [])) if pre_cart_res.ok else set()
            except Exception:
                pre_cart_ids = set()

            with ThreadPoolExecutor(max_workers=5) as executor:
                futures = {
                    executor.submit(
                        fetch_single_po,
                        self.session,
                        po,
                        brand,
                        product_gender,
                        product_type,
                        franchise,
                        vendor_po,
                        season,
                        price_tickets,
                        price_ticket_code,
                        order_lpn_labels,
                        order_type,
                        token
                    ): po for po in clean_po_list
                }

                for future in as_completed(futures):
                    po = futures[future]
                    try:
                        po_num, success, error_msg = future.result()
                        if success:
                            yield f"{po_num} job add to cart"
                            successful_pos.append(po_num)
                        else:
                            yield f"WARNING: Failed to process PO {po_num} - {error_msg}"
                            missing_pos.append(po_num)
                    except Exception as exc:
                        yield f"WARNING: Exception for PO {po} - {str(exc)}"
                        missing_pos.append(po)

            if not successful_pos:
                yield "ERROR: No valid PO details found to process."
                return None, successful_pos, missing_pos

            cart_res = self.session.get("https://gapbrands.r-pac.com.hk/api/app/shopping-cart")
            if not cart_res.ok:
                yield f"ERROR: Failed to retrieve cart: {cart_res.text}"
                return None, successful_pos, missing_pos
                
            all_cart_items = cart_res.json().get("items", [])
            # Only use the items we just added (not stale items from previous runs)
            cart_items = [ci for ci in all_cart_items if ci.get("cartId") not in pre_cart_ids]
            if not cart_items:
                # Fallback: use all items if tracking failed
                cart_items = all_cart_items
            if not cart_items:
                yield "ERROR: Shopping cart is empty despite successful additions."
                return None, successful_pos, missing_pos

            # Patch cart items: inject cooId for GAP brand (required by server validation)
            COO_MADE_IN_SRI_LANKA = "d2c28254-45b4-4b55-8677-6c82668f7a46"
            for ci in cart_items:
                gap_dto = ci.get("cartGapPoItemDto")
                if gap_dto is not None:
                    ci_brand = gap_dto.get("brand", "")
                    if ci_brand and ci_brand.lower() not in ("oldnavy", "old navy"):
                        if not gap_dto.get("cooId"):
                            gap_dto["cooId"] = COO_MADE_IN_SRI_LANKA

            # Enforce LPN slab quantities on the cart items before checkout
            if order_lpn_labels == "YES":
                for ci in cart_items:
                    # Also patch the top level quantity in the cart item!
                    for dto_key in ["cartGapPoItemDto", "cartOldNavyPoItemDto"]:
                        dto = ci.get(dto_key)
                        if dto is not None:
                            try:
                                base_qty = int(float(dto.get("originalQuantity", dto.get("quantity", 0))))
                                if base_qty == 0:
                                    base_qty = int(float(ci.get("quantity", 0)))
                                
                                item_brand = dto.get("brand") or brand
                                if base_qty > 0:
                                    new_qty = str(calculate_lpn_quantity(base_qty, order_type, item_brand))
                                    dto["lpnQuantity"] = new_qty
                                    dto["quantity"] = new_qty
                                    # Very important: Update the top-level cart item properties!
                                    ci["quantity"] = new_qty
                                    ci["lpnQuantity"] = new_qty
                            except (ValueError, TypeError):
                                pass

            # Confirm 1st
            form_data = to_form_urlencoded(cart_items, token)
            headers = {
                "content-type": "application/x-www-form-urlencoded; charset=UTF-8",
                "x-requested-with": "XMLHttpRequest"
            }
            conf1_url = "https://gapbrands.r-pac.com.hk/Main/ShoppingCart/Confirm"
            conf1_res = self.session.post(conf1_url, data=form_data, headers=headers)
            if not conf1_res.ok:
                yield f"WARNING: Confirm 1st failed with status {conf1_res.status_code}"

            # Confirm 2nd
            checkout_payload = {
                "confirmDto": {
                    "userShipBillId": "3a0ec547-277d-bfa1-b124-e5a138679563",
                    "printShopId": "3a0cc21c-7ffe-1270-cb6a-7649de5278d0",
                    "deliveryMethod": "0",
                    "courier": "no",
                    "account": "",
                    "specialRequest": "",
                    "customerPo": "",
                    "vendorPo": "",
                    "cartItemDtos": cart_items
                },
                "__RequestVerificationToken": token
            }
            
            # Get latest global job ID BEFORE checkout to prevent overlap with older test jobs
            global_latest_id = 0
            try:
                import time
                hist_res = self.session.get(f"https://gapbrands.r-pac.com.hk/api/app/order?orderType=All&sorting=id%20desc&skipCount=0&maxResultCount=1&_t={int(time.time() * 1000)}")
                if hist_res.ok:
                    items = hist_res.json().get("items", [])
                    if items:
                        global_latest_id = items[0].get("id", 0)
            except Exception:
                pass
                
            # Use order/common for all brands (as confirmed from browser network capture)
            checkout_url = "https://gapbrands.r-pac.com.hk/api/app/order/common"
            checkout_res = self.session.post(
                checkout_url,
                json=checkout_payload,
                headers={
                    "X-XSRF-TOKEN": token,
                    "x-requested-with": "XMLHttpRequest"
                }
            )
            if not checkout_res.ok:
                yield f"ERROR: Checkout failed: {checkout_res.text}"
                return None, successful_pos, missing_pos

            yield "job created"
            job_ids = []

            yield "Checking global history fallback to locate new Job IDs..."
            from datetime import datetime, timedelta, timezone
            # Allow a 5-minute window for the server time difference
            checkout_start_time = datetime.now(timezone.utc) - timedelta(minutes=5)
            
            import urllib.parse
            for po in successful_pos:
                import time
                history_url = f"https://gapbrands.r-pac.com.hk/api/app/order?customerPo={urllib.parse.quote(po)}&orderType=All&sorting=id%20desc&skipCount=0&maxResultCount=10&_t={int(time.time() * 1000)}"
                hist_res = self.session.get(history_url)
                if hist_res.ok:
                    items = hist_res.json().get("items", [])
                    for item in items:
                        # STRICT MATCHING: Check exact PO No
                        item_po = item.get("poNo") or item.get("customerPo") or item.get("vendorPo") or ""
                        if item_po != po:
                            continue
                            
                        # STRICT MATCHING: Check exact Vendor PO
                        if vendor_po and item.get("vendorPo") != vendor_po:
                            continue
                            
                        j_id = str(item.get("id"))
                        ctime_str = item.get("creationTime")
                        if ctime_str:
                            try:
                                # Parse server time (e.g., '2026-08-06T04:05:12.347827Z')
                                ctime = datetime.fromisoformat(ctime_str.replace("Z", "+00:00"))
                                is_new = False
                                if global_latest_id > 0:
                                    is_new = int(j_id) > global_latest_id
                                else:
                                    is_new = ctime > checkout_start_time
                                    
                                if is_new:
                                    if j_id and j_id not in job_ids and j_id != "None":
                                        job_ids.append(j_id)
                            except Exception:
                                # Fallback if parsing fails, just take the first one
                                if j_id and j_id not in job_ids and j_id != "None":
                                    job_ids.append(j_id)
                                    break
            
            if not job_ids:
                yield "ERROR: No Job IDs found in checkout response or fallback."
                return None, successful_pos, missing_pos

            zip_buffer = io.BytesIO()
            valid_files = 0

            with zipfile.ZipFile(zip_buffer, "w", zipfile.ZIP_DEFLATED) as zip_file:
                with ThreadPoolExecutor(max_workers=5) as executor:
                    futures = {executor.submit(download_single_job, self.session, jid, brand): jid for jid in job_ids}

                    for future in as_completed(futures):
                        jid = futures[future]
                        try:
                            job_id, success, filename, content, error_msg = future.result()
                            if success:
                                zip_file.writestr(filename, content)
                                valid_files += 1
                                yield "job downloaded"
                            else:
                                yield f"WARNING: Failed download for Job {job_id} - {error_msg}"
                        except Exception as exc:
                            yield f"WARNING: Exception download for Job {jid} - {str(exc)}"

            if valid_files == 0:
                file_result = None
                yield "WARNING: Failed to download any order files."
            else:
                file_result = zip_buffer.getvalue()
                yield "zip ready"

        except Exception as e:
            yield f"ERROR: Failed during process. {str(e)}"

        yield "DONE"
        return file_result, successful_pos, missing_pos
