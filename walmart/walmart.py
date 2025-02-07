# -*- coding: utf-8 -*-
import requests
import uuid
import csv
import io
import zipfile

from datetime import datetime
from requests.auth import HTTPBasicAuth
from lxml import etree
from lxml.builder import E, ElementMaker
import xml.etree.ElementTree as ET

from .exceptions import WalmartAuthenticationError, WalmartException


def epoch_milliseconds(dt):
    "Walmart accepts timestamps as epoch time in milliseconds"
    epoch = datetime.utcfromtimestamp(0)
    return int((dt - epoch).total_seconds() * 1000.0)


class Walmart(object):

    def __init__(self, client_id, client_secret, headers=None):
        """To get client_id and client_secret for your Walmart Marketplace
        visit: https://developer.walmart.com/#/generateKey
        """
        self.client_id = client_id
        self.client_secret = client_secret
        self.token = None
        self.token_expires_in = None
        self.base_url = "https://marketplace.walmartapis.com/v3"

        session = requests.Session()
        session.headers.update({
            "WM_SVC.NAME": "Walmart Marketplace",
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "application/json",
        })
        if headers: # additional headers if required to pass
            session.headers.update(headers) 
        session.auth = HTTPBasicAuth(self.client_id, self.client_secret)
        self.session = session

        # Get the token required for API requests
        self.authenticate()

    def authenticate(self):
        data = self.send_request(
            "POST", "{}/token".format(self.base_url),
            body={
                "grant_type": "client_credentials",
            },
        )
        self.token = data["access_token"]
        self.token_expires_in = data["expires_in"]

        self.session.headers["WM_SEC.ACCESS_TOKEN"] = self.token

    @property
    def items(self):
        return Items(connection=self)

    @property
    def inventory(self):
        return Inventory(connection=self)

    @property
    def prices(self):
        return Prices(connection=self)

    @property
    def orders(self):
        return Orders(connection=self)

    @property
    def report(self):
        return Report(connection=self)
    
    @property
    def report_request(self):
        return ReportRequest(connection=self)

    @property
    def feed(self):
        return Feed(connection=self)
    
    @property
    def returns(self):
        return Returns(connection=self)
    
    @property
    def fulfillment(self):
        return Fulfillment(connection=self)

    def send_request(
        self, method, url, params=None, body=None, json=None,
        request_headers=None, octet_stream=False
    ):
        # A unique ID which identifies each API call and used to track
        # and debug issues; use a random generated GUID for this ID
        headers = {
            "WM_QOS.CORRELATION_ID": uuid.uuid4().hex,
        }
        if request_headers:
            headers.update(request_headers)
        if octet_stream:
            headers["Accept"] = "application/octet-stream"
            

        response = None
        if method == "GET":
            response = self.session.get(url, params=params, headers=headers)
        elif method == "PUT":
            response = self.session.put(
                url, params=params, headers=headers, data=body
            )
        elif method == "POST":
            request_params = {
                "params": params,
                "headers": headers,
            }
            if json is not None:
                request_params["json"] = json
            else:
                request_params["data"] = body
            response = self.session.post(url, **request_params)

        if response is not None:
            try:
                response.raise_for_status()
            except requests.exceptions.HTTPError:
                if response.status_code == 401:
                    raise WalmartAuthenticationError((
                        "Invalid client_id or client_secret. Please verify "
                        "your credentials from https://developer.walmart."
                        "com/#/generateKey"
                    ))
                elif response.status_code == 400:
                    try:
                        data = response.json()
                    except Exception as ex:
                        root = ET.fromstring(response.text)

                        # Iterate through error elements
                        data = {}
                        data["error"] = []
                        for error in root.findall('error'):
                            data["error"].append({"code":error.find('code').text})
                    if "error" in data and data["error"][0]["code"] == \
                            "INVALID_TOKEN.GMP_GATEWAY_API":
                        # Refresh the token as the current token has expired
                        self.authenticate()
                        return self.send_request(
                            method, url, params, body, request_headers
                        )
                    elif "error" in data and "NO_REPORT" in response.text:
                        return [], None
                raise
        try:
            return response.json()
        except ValueError:
            # In case of reports, there is no JSON response, so return the
            # content instead which contains the actual report
            if octet_stream:
                return response.content, response.headers.get("Content-Disposition") #filename is present in header
            return response.content


class Resource(object):
    """
    A base class for all Resources to extend
    """

    def __init__(self, connection):
        self.connection = connection

    @property
    def url(self):
        return "{}/{}".format(self.connection.base_url, self.path)

    def all(self, **kwargs):
        return self.connection.send_request(
            method="GET", url=self.url, params=kwargs
        )

    def get(self, id):
        url = "{}/{}".format(self.url, id)
        return self.connection.send_request(method="GET", url=url)

    def update(self, **kwargs):
        return self.connection.send_request(
            method="PUT", url=self.url, params=kwargs
        )


class Items(Resource):
    """
    Get all items
    """

    path = 'items'

    def get_items(self):
        "Get all the items from the Item Report"
        response = self.connection.report.all(type="item")
        zf = zipfile.ZipFile(io.BytesIO(response), "r")
        product_report = zf.read(zf.infolist()[0]).decode("utf-8")

        return list(csv.DictReader(io.StringIO(product_report)))
    
    def search(self, **kwargs): # search method to hit request to search endpoint with keywords
        url = self.url + "/walmart/search"
        return self.connection.send_request(
            method="GET", url=url, params=kwargs
        )
    
    def get_taxonomy(self): # taxonomy method to hit request to taxonomy endpoint
        url = self.url + "/taxonomy"
        return self.connection.send_request(
            method="GET", url=url
        )


class Inventory(Resource):
    """
    Retreives inventory of an item
    """

    path = 'inventory'
    feedType = 'inventory'

    def bulk_update(self, items):
        """Updates the inventory for multiple items at once by creating the
        feed on Walmart.

        :param items: Items for which the inventory needs to be updated in
        the format of:
            [{
                "sku": "XXXXXXXXX",
                "availability_code": "AC",
                "quantity": "10",
                "uom": "EACH",
                "fulfillment_lag_time": "1",
            }]
        """
        inventory_data = []
        for item in items:
            data = {
                "sku": item["sku"],
                "quantity": {
                    "amount": item["quantity"],
                    "unit": item.get("uom", "EACH"),
                },
                "fulfillmentLagTime": item.get("fulfillment_lag_time"),
            }
            if item.get("availability_code"):
                data["availabilityCode"] = item["availability_code"]
            inventory_data.append(data)

        body = {
            "InventoryHeader": {
                "version": "1.4",
            },
            "Inventory": inventory_data,
        }
        return self.connection.feed.create(resource="inventory", content=body)

    def update_inventory(self, sku, quantity):
        headers = {
            'Content-Type': "application/xml"
        }
        return self.connection.send_request(
            method='PUT',
            url=self.url,
            params={'sku': sku},
            body=self.get_inventory_payload(sku, quantity),
            request_headers=headers
        )
    
    def get_multiple_item_inventory_for_all_ship_nodes(self, **kwargs):
        url = self.url.replace("inventory", "inventories")
        return self.connection.send_request(
            method="GET",
            url=url,
            params=kwargs
        )

    def get_inventory_payload(self, sku, quantity):
        element = ElementMaker(
            namespace='http://walmart.com/',
            nsmap={
                'wm': 'http://walmart.com/',
            }
        )
        return etree.tostring(
            element(
                'inventory',
                element('sku', sku),
                element(
                    'quantity',
                    element('unit', 'EACH'),
                    element('amount', str(quantity)),
                ),
                element('fulfillmentLagTime', '4'),
            ), xml_declaration=True, encoding='utf-8'
        )

    def get_payload(self, items):
        return etree.tostring(
            E.InventoryFeed(
                E.InventoryHeader(E('version', '1.4')),
                *[E(
                    'inventory',
                    E('sku', item['sku']),
                    E(
                        'quantity',
                        E('unit', 'EACH'),
                        E('amount', item['quantity']),
                    )
                ) for item in items],
                xmlns='http://walmart.com/'
            )
        )


class Prices(Resource):
    """
    Retreives price of an item
    """

    path = 'prices'
    feedType = 'price'

    def get_repricer_strategies(self, **kwargs):
        url = self.url.replace("prices", "repricer") + "/strategies"
        return self.connection.send_request(
            method="GET",
            url=url,
            params=kwargs
        )
    
    def get_promotional_prices(self, sku):
        url = self.url.replace("prices", "promo")+f"/sku/{sku}"
        return self.connection.send_request(
            method="GET",
            url=url
        )

    def get_payload(self, items):
        root = ElementMaker(
            nsmap={'gmp': 'http://walmart.com/'}
        )
        return etree.tostring(
            root.PriceFeed(
                E.PriceHeader(E('version', '1.5')),
                *[E.Price(
                    E(
                        'itemIdentifier',
                        E('sku', item['sku'])
                    ),
                    E(
                        'pricingList',
                        E(
                            'pricing',
                            E(
                                'currentPrice',
                                E(
                                    'value',
                                    **{
                                        'currency': item['currenctCurrency'],
                                        'amount': item['currenctPrice']
                                    }
                                )
                            ),
                            E('currentPriceType', item['priceType']),
                            E(
                                'comparisonPrice',
                                E(
                                    'value',
                                    **{
                                        'currency': item['comparisonCurrency'],
                                        'amount': item['comparisonPrice']
                                    }
                                )
                            ),
                            E(
                                'priceDisplayCode',
                                **{
                                    'submapType': item['displayCode']
                                }
                            ),
                        )
                    )
                ) for item in items]
            ), xml_declaration=True, encoding='utf-8'
        )


class Orders(Resource):
    """
    Retrieves Order details
    """

    path = 'orders'

    def all(self, **kwargs):
        try:
            return super(Orders, self).all(**kwargs)
        except requests.exceptions.HTTPError as exc:
            if exc.response.status_code == 404:
                # If no orders are there on walmart matching the query
                # filters, it throws 404. In this case return an empty
                # list to make the API consistent
                return {
                    "list": {
                        "elements": {
                            "order": [],
                        }
                    }
                }
            raise
    
    def get_released_orders(self, **kwargs): # method to hit released orders api
        url = self.url + '/released'
        return self.connection.send_request(method='GET', url=url, params=kwargs)

    def acknowledge(self, id):
        url = self.url + '/%s/acknowledge' % id
        return self.send_request(method='POST', url=url)

    def cancel(self, id, lines):
        url = self.url + '/%s/cancel' % id
        return self.send_request(
            method='POST', url=url, data=self.get_cancel_payload(lines))

    def get_cancel_payload(self, lines):
        element = ElementMaker(
            namespace='http://walmart.com/mp/orders',
            nsmap={
                'ns2': 'http://walmart.com/mp/orders',
                'ns3': 'http://walmart.com/'
            }
        )
        return etree.tostring(
            element(
                'orderCancellation',
                element(
                    'orderLines',
                    *[element(
                        'orderLine',
                        element('lineNumber', line),
                        element(
                            'orderLineStatuses',
                            element(
                                'orderLineStatus',
                                element('status', 'Cancelled'),
                                element(
                                    'cancellationReason', 'CANCEL_BY_SELLER'),
                                element(
                                    'statusQuantity',
                                    element('unitOfMeasurement', 'EACH'),
                                    element('amount', '1')
                                )
                            )
                        )
                    ) for line in lines]
                )
            ), xml_declaration=True, encoding='utf-8'
        )

    def create_shipment(self, order_id, lines):
        """Send shipping updates to Walmart

        :param order_id: Purchase order ID of an order
        :param lines: Order lines to be fulfilled in the format:
            [{
                "line_number": "123",
                "uom": "EACH",
                "quantity": 3,
                "ship_time": datetime(2019, 04, 04, 12, 00, 00),
                "other_carrier": None,
                "carrier": "USPS",
                "carrier_service": "Standard",
                "tracking_number": "34567890567890678",
                "tracking_url": "www.fedex.com",
            }]
        """
        url = self.url + "/{}/shipping".format(order_id)

        order_lines = []
        for line in lines:
            ship_time = line.get("ship_time", "")
            if ship_time:
                ship_time = epoch_milliseconds(ship_time)
            order_lines.append({
                "lineNumber": line["line_number"],
                "orderLineStatuses": {
                    "orderLineStatus": [{
                        "status": "Shipped",
                        "statusQuantity": {
                            "unitOfMeasurement": line.get("uom", "EACH"),
                            "amount": str(int(line["quantity"])),
                        },
                        "trackingInfo": {
                            "shipDateTime": ship_time,
                            "carrierName": {
                                "otherCarrier": line.get("other_carrier"),
                                "carrier": line["carrier"],
                            },
                            "methodCode": line.get("carrier_service", ""),
                            "trackingNumber": line["tracking_number"],
                            "trackingURL": line.get("tracking_url", "")
                        }
                    }],
                }
            })

        body = {
            "orderShipment": {
                "orderLines": {
                    "orderLine": order_lines,
                }
            }
        }
        return self.connection.send_request(
            method="POST",
            url=url,
            json=body,
        )


class Report(Resource):
    """
    Get report
    """

    path = 'getReport'

class ReportRequest(Resource):
    path = "reports"

    def create_report_request(self, report_type, report_version):
        url = self.url + "/reportRequests"
        return self.connection.send_request(
            method="POST",
            url=url,
            params={
                "reportType":report_type,
                "reportVersion": report_version
            }
        )

    def get_report_request_status(self, request_id):
        url = self.url + f"/reportRequests/{request_id}"
        return self.connection.send_request(
            method="GET",
            url=url
        )
    
    def get_download_report_url(self, request_id):
        url = self.url + f"/downloadReport"
        return self.connection.send_request(
            method="GET",
            url=url,
            params={"requestId":request_id}
        )

    def download_recon_report(self, report_date, report_version="v1"):
        url=self.url.replace("reports", "report")+"/reconreport/reconFile"
        return self.connection.send_request(
            method="GET",
            url=url,
            params={"reportDate":report_date, "reportVersion":report_version},
            octet_stream=True
        )

class Feed(Resource):
    path = "feeds"

    def create(self, resource, content):
        """Creates the feed on Walmart for respective resource

        Once you upload the Feed, you can use the Feed ID returned in the
        response to track the status of the feed and the status of the
        item within that Feed.

        :param resource: The resource for which the feed needs to be created.
        :param content: The content needed to create the Feed.
        """
        return self.connection.send_request(
            method="POST",
            url=self.url,
            params={
                "feedType": resource,
            },
            json=content,
        )

    def get_status(self, feed_id, offset=0, limit=1000):
        "Returns the feed and item status for a specified Feed ID"
        return self.connection.send_request(
            method="GET",
            url="{}/{}".format(self.url, feed_id),
            params={
                "includeDetails": "true",
                "limit": limit,
                "offset": offset,
            },
        )



class Returns(Resource):
    """
    Get information about returns
    """

    path = 'returns'

    def get_return_details(self, **kwargs):
        url = self.url
        return self.connection.send_request(
            method="GET",
            url=url,
            params=kwargs
        )
    

class Fulfillment(Resource):
    """
        Get information about fulfillment
    """

    path = 'fulfillment'

    def get_wfs_inventory(self, **kwargs):
        url = self.url + "/inventory"
        return self.connection.send_request(
            method="GET",
            url=url,
            params=kwargs
        )
    
    def get_wfs_orders(self, **kwargs):
        url = self.url.replace("fulfillment", "orders")
        params = {"shipNodeType":"WFSFulfilled"}
        if kwargs:
            params.update(kwargs)
        return self.connection.send_request(
            method="GET",
            url=url,
            params=params
        )
    
    def get_inbound_shipment(self, shipment_id):
        url = self.url + "/inbound-shipments"
        return self.connection.send_request(
            method="GET",
            url=url,
            params={"shipmentId":shipment_id}
        )
    
    def get_inbound_shipment_items(self, shipment_id):
        url = self.url + "/inbound-shipment-items"
        return self.connection.send_request(
            method="GET",
            url=url,
            params={"shipmentId":shipment_id}
        )
    
    def get_wfs_inventory_health_report(self):
        url = self.url.replace("fulfillment", "report")+"/wfs/getInventoryHealthReport"
        # headers = {
        #     'Accept': "application/octet-stream"
        # }
        return self.connection.send_request(
            method="GET",
            url=url
            # request_headers = headers
        )
    
    def get_shipments(self, **kwargs):
        url = self.url + "/inbound-shipments"
        return self.connection.send_request(
            method="GET",
            url=url,
            params=kwargs
        )
    
    def get_inventory_log(self, **kwargs):
        url = self.url + "/inventory-log"
        return self.connection.send_request(
            method = "GET",
            url = url,
            params=kwargs
        )
    
    def get_carrier_quotes(self, **kwargs):
        url = self.url + "/carrier-rate-quotes"
        return self.connection.send_request(
            method = "GET",
            url = url,
            params=kwargs
        ) 
    


