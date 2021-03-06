#!/usr/bin/env python
# -*- coding: utf-8 -*-
#
# Basic interface to Amazon MWS
# Based on http://code.google.com/p/amazon-mws-python

# import urllib
try:
    from urllib.parse import quote as url_quote
except ImportError:
    # Python 2 version: quote in urllib directly, not in a parse module
    from urllib import quote as url_quote
import hashlib
import hmac
import base64
import re
try:
    from xml.etree.ElementTree import ParseError as XMLError
except ImportError:
    from xml.parsers.expat import ExpatError as XMLError
from datetime import datetime

from requests import request # pylint: disable=E0401
# from requests.exceptions import HTTPError

from . import utils


__all__ = [
    'Feeds',
    'Inventory',
    'MWSError',
    'Reports',
    'Orders',
    'Products',
    'Recommendations',
    'Sellers',
    'InboundShipments',
]

# List of MWS endpoints and MarketplaceId values:
# http://docs.developer.amazonservices.com/en_US/dev_guide/DG_Endpoints.html
MARKETPLACES = {
    "CA" : "https://mws.amazonservices.ca", #A2EUQ1WTGCTBG2
    "US" : "https://mws.amazonservices.com", #ATVPDKIKX0DER
    "DE" : "https://mws-eu.amazonservices.com", #A1PA6795UKMFR9
    "ES" : "https://mws-eu.amazonservices.com", #A1RKKUPIHCS9HS
    "FR" : "https://mws-eu.amazonservices.com", #A13V1IB3VIYZZH
    "IN" : "https://mws.amazonservices.in", #A21TJRUUN4KGV
    "IT" : "https://mws-eu.amazonservices.com", #APJ6JRA9NG5V4
    "UK" : "https://mws-eu.amazonservices.com", #A1F83G8C2ARO7P
    "JP" : "https://mws.amazonservices.jp", #A1VC38T7YXB528
    "CN" : "https://mws.amazonservices.com.cn", #AAHKV2X7AFYLW
    "MX" : "https://mws.amazonservices.com.mx", #A1AM78C64UM0Y8
}


class MWSError(Exception):
    """
    Main MWS Exception class
    """
    # Allows quick access to the response object.
    # Do not rely on this attribute, always check if its not None.
    response = None


def calc_md5(string):
    """
    Calculates the MD5 encryption for the given string
    """
    md5_hash = hashlib.md5()
    md5_hash.update(string)
    # The below should be 'encodebytes' in Python3, as 'encodestring'
    # is a deprecated alias for that method.
    # Since it still works and remains backwards-compatible, I'm leaving it here.
    return base64.encodestring(md5_hash.digest()).strip(b'\n')


def remove_empty(dict_obj):
    """
    Helper function that returns a copy of a dictionary,
    excluding keys with empty values.
    """
    return {k: v for k, v in dict_obj.items() if v}


def dt_iso_or_none(dt_obj):
    """
    If dt_obj is a datetime, return isoformat()
    TODO: if dt_obj is a string in iso8601 already, return it back
    Otherwise, return None
    """
    # If d is a datetime object, format it to iso and return
    if isinstance(dt_obj, datetime):
        return dt_obj.isoformat()

    # TODO: if dt_obj is a string in iso8601 already, return it

    # none of the above: return None
    return None


def remove_namespace(xml):
    regex = re.compile(' xmlns(:ns2)?="[^"]+"|(ns2:)|(xml:)')
    try:
        out = regex.sub('', xml)
    except TypeError:
        out = regex.sub('', xml.decode('utf-8'))
    return out


def unique_list_order_preserved(seq):
    """
    Returns a unique list of items from the sequence
    while preserving original ordering.
    The first occurence of an item is returned in the new sequence:
    any subsequent occurrences of the same item are ignored.
    """
    seen = set()
    seen_add = seen.add
    return [x for x in seq if not (x in seen or seen_add(x))]


class DictWrapper(object):
    def __init__(self, xml, rootkey=None):
        self.original = xml
        self.response = None
        self.timestamp = None
        self._rootkey = rootkey
        self._mydict = utils.xml2dict().fromstring(remove_namespace(xml))
        self._response_dict = self._mydict.get(
            list(self._mydict.keys())[0],
            self._mydict
        )

        # Pull out list of "invalid_items" as a list of dicts.
        # (this gets sent as a flat string, so we need to parse it)
        self.invalid_items = None
        if self.is_error():
            message = ''
            if 'Error' in self._response_dict:
                message = self._response_dict.Error.get('Message', '')
                if message:
                    message = message['value']

            invalid_pattern = re.compile(r'(?<=InvalidItems\[)\s?(.*?)(?=\]\.)')

            dict_pattern = re.compile(r'(skuType|sku|reason)=(.*?),?\s?(?=skuType|sku|reason|$)')

            match = invalid_pattern.search(message)
            if match:
                item_lines = match.group().strip('() ').split('), (')

                results = []
                for line in item_lines:
                    result = dict(dict_pattern.findall(line))
                    result = {k: v for k, v in result.items()}
                    results.append(result)

                self.invalid_items = results or None


    @property
    def parsed(self):
        root = None
        if self._rootkey:
            root = self._response_dict.get(self._rootkey)
        if root is None:
            root = self._response_dict

        return root


    @property
    def request_id(self):
        metadata = self._response_dict.get('ResponseMetadata')
        if metadata:
            r_id = metadata.get('RequestId')
        else:
            r_id = self._response_dict.get('RequestId')
        if hasattr(r_id, 'value'):
            return r_id['value']
        return None


    @property
    def error(self):
        """
        Return the Error element in the response, if it exists.
        """
        if 'Error' in self._response_dict:
            error_dict = self._response_dict.Error
            if 'Message' not in error_dict:
                error_dict['Message'] = error_dict.get('Code')
            return error_dict
        return None


    def is_error(self):
        return bool(self.error)


    def is_throttled(self):
        if not self.is_error():
            return False
        code = self.error.get('Code', None)
        code = code['value'] if code else None
        return code == 'RequestThrottled'


class DataWrapper(object):
    """
    Text wrapper in charge of validating the hash sent by Amazon.
    """
    def __init__(self, data, header):
        self.original = data
        self.response = None
        self.timestamp = None
        if 'content-md5' in header:
            hash_ = calc_md5(self.original)
            if header['content-md5'] != hash_:
                raise MWSError("Wrong Contentlength, maybe amazon error...")


    @property
    def parsed(self):
        return self.original


class MWS(object):
    """
    Base Amazon API class
    """

    # This is used to post/get to the different uris used by amazon per api
    # ie. /Orders/2011-01-01
    # All subclasses must define their own URI only if needed
    URI = "/"

    # The API version varies in most amazon APIs
    VERSION = "2009-01-01"

    # There seem to be some xml namespace issues. therefore every api subclass
    # is recommended to define its namespace, so that it can be referenced
    # like so AmazonAPISubclass.NAMESPACE.
    # For more information see http://stackoverflow.com/a/8719461/389453
    NAMESPACE = ''

    # In here we name each of the operations available to the subclass
    # that have 'ByNextToken' operations associated with them.
    # If the Operation is not listed here, self.action_by_next_token
    # will raise an error.
    NEXT_TOKEN_OPERATIONS = []

    # Some APIs are available only to either a "Merchant" or "Seller"
    # the type of account needs to be sent in every call to the amazon MWS.
    # This constant defines the exact name of the parameter Amazon expects
    # for the specific API being used.
    # All subclasses need to define this if they require another account type
    # like "Merchant" in which case you define it like so.
    # ACCOUNT_TYPE = "Merchant"
    # Which is the name of the parameter for that specific account type.
    ACCOUNT_TYPE = "SellerId"


    ACTION_BY_NEXT_TOKEN_DEPRECATION_WARNING = (
        "WARNING: this method has been deprecated. Please use "
        "`MWS.action_by_next_token` in the future."
    )


    def __init__(self, access_key, secret_key, account_id,
                 region='US', domain='', uri="", version="", auth_token=""):
        self.access_key = access_key
        self.secret_key = secret_key
        self.account_id = account_id
        self.auth_token = auth_token
        self.version = version or self.VERSION
        self.uri = uri or self.URI

        if domain:
            self.domain = domain
        elif region in MARKETPLACES:
            self.domain = MARKETPLACES[region]
        else:
            error_msg = (
                "Incorrect region supplied ('{region}'). Must be one "
                "of the following: {marketplaces}"
            ).format(
                marketplaces=', '.join(MARKETPLACES.keys()),
                region=region,
            )
            raise MWSError(error_msg)


    def make_request(self, extra_data, method="GET", **kwargs):
        """
        Make request to Amazon MWS API with these parameters
        """

        # Remove all keys with an empty value because
        # Amazon's MWS does not allow such a thing.
        extra_data = remove_empty(extra_data)
        utc_now = datetime.utcnow()

        params = {
            'AWSAccessKeyId': self.access_key,
            self.ACCOUNT_TYPE: self.account_id,
            'SignatureVersion': '2',
            'Timestamp': utc_now.isoformat(),
            'Version': self.version,
            'SignatureMethod': 'HmacSHA256',
        }
        if self.auth_token:
            params['MWSAuthToken'] = self.auth_token
        params.update(extra_data)
        request_description = '&'.join([
            '{key}={value}'.format(
                key=k,
                value=url_quote(params[k], safe='-_.~'),
            ) for k in sorted(params)
        ])
        signature = self.calc_signature(method, request_description)
        url = '{domain}{uri}?{description}&Signature={signature}'.format(
            domain=self.domain,
            uri=self.uri,
            description=request_description,
            signature=url_quote(signature),
        )
        headers = {'User-Agent': 'python-amazon-mws/0.0.1 (Language=Python)'}
        headers.update(kwargs.get('extra_headers', {}))

        # try:
        # Some might wonder as to why I don't pass the params dict
        # as the params argument to request. My answer is, here I have
        # to get the url parsed string of params in order to sign it, so
        # if I pass the params dict as params to request, request will
        # repeat that step because it will need to convert the dict to
        # a url parsed string, so why do it twice if I can just pass
        # the full url :).
        response = request(
            method,
            url,
            data=kwargs.get('body', ''),
            headers=headers,
        )
        # response.raise_for_status()
        # When retrieving data from the response object,
        # be aware that response.content returns the content in bytes
        # while response.text calls response.content and
        # converts it to unicode.
        data = response.content

        # I do not check the headers to decide which content structure
        # to server simply because sometimes Amazon's MWS API returns
        # XML error responses with "text/plain" as the Content-Type.
        try:
            parsed_response = DictWrapper(
                data, extra_data.get("Action") + "Result"
            )
        except XMLError:
            parsed_response = DataWrapper(data, response.headers)

        # except HTTPError as e:
            # error = MWSError(str(e))
            # error.response = e.response
            # raise e

        # Store the response object in the parsed_response for quick access
        parsed_response.response = response
        # MWS recommends saving timestamp, so we make it available.
        parsed_response.timestamp = utc_now
        return parsed_response


    def get_service_status(self):
        """
        Returns a GREEN, GREEN_I, YELLOW or RED status,
        depending on the status/availability of the API
        it's being called from.
        """
        return self.make_request(extra_data=dict(Action='GetServiceStatus'))


    def action_by_next_token(self, action, next_token):
        """
        Run a '...ByNextToken' action for the given action.
        If the action is not listed in self.NEXT_TOKEN_OPERATIONS,
        MWSError will be raised.
        Action is expected NOT to include 'ByNextToken'
        at the end of its name for this call: function will add that
        by itself.
        """
        if action not in self.NEXT_TOKEN_OPERATIONS:
            raise MWSError((
                "{} action not listed in this API's NEXT_TOKEN_OPERATIONS. "
                "Please refer to documentation."
            ).format(action))

        action = '{}ByNextToken'.format(action)

        data = dict(
            Action=action,
            NextToken=next_token
        )
        return self.make_request(data, method="POST")


    def calc_signature(self, method, request_description):
        """
        Calculate MWS signature to interface with Amazon
        """
        sig_data = '\n'.join([
            method,
            self.domain.replace('https://', '').lower(),
            self.uri,
            request_description
        ])
        return base64.b64encode(
            hmac.new(
                str(self.secret_key).encode('utf-8'),
                sig_data.encode('utf-8'),
                hashlib.sha256
            ).digest()
        )


    def _enumerate_param(self, param, values):
        """
        Builds a dictionary of an enumerated parameter.
        Takes any iterable and returns a dictionary.
        example:
          _enumerate_param('MarketplaceIdList.Id', (123, 345, 4343))
        returns:
          {
              MarketplaceIdList.Id.1: 123,
              MarketplaceIdList.Id.2: 345,
              MarketplaceIdList.Id.3: 4343
          }
        """
        # Shortcut for empty values
        if not values:
            return {}

        if not isinstance(values, list) and not isinstance(values, tuple):
            values = [values,]

        # Ensure this enumerated param ends in '.'
        if not param.endswith('.'):
            param += '.'

        # Return a dict comprehension of the param, enumerated,
        # with its associated values.
        return {
            '{}{}'.format(param, idx+1): val
            for idx, val in enumerate(values)
        }


    def enumerate_params(self, params=None):
        """
        Takes a dict of params:
            each key is a param to be enumerated
            each value is a list of values for that param.
        For each param and values, runs _enumerate_param,
        returning a flat dict of all results
        """
        if params is None or not isinstance(params, dict):
            return {}

        params_output = {}
        for param, values in params.items():
            params_output.update(self._enumerate_param(param, values))

        return params_output


    def enumerate_keyed_param(self, param, values):
        """
        Takes a parameter and a list of dicts of values. Each dict in the list
        - Example:
        param = "InboundShipmentPlanRequestItems.member"
        values = [
            {'SellerSKU': 'Football2415',
             'Quantity': 3},
            {'SellerSKU': 'TeeballBall3251',
             'Quantity': 5},
            ...
        ]

        output = {
            'InboundShipmentPlanRequestItems.member.1.SellerSKU': 'Football2415',
            'InboundShipmentPlanRequestItems.member.1.Quantity': 3,
            'InboundShipmentPlanRequestItems.member.2.SellerSKU': 'TeeballBall3251',
            'InboundShipmentPlanRequestItems.member.2.Quantity': 5,
        }
        """
        if not values:
            # Shortcut for empty values
            return {}

        if not param.endswith('.'):
            # Ensure the enumerated param ends in '.'
            param += '.'

        if not isinstance(values, list) and not isinstance(values, tuple):
            # If it's a single value, convert it to a list first
            values = [values,]

        if not isinstance(values[0], dict):
            # Value is not a dict: can't work on it here.
            raise MWSError((
                "Values must be in the form of either a list or "
                "tuple of dictionaries."
            ))

        params = {}
        for idx, val_dict in enumerate(values):
            params.update({
                '{param}{idx}.{key}'.format(param=param, idx=idx+1, key=k): v
                for k, v in val_dict.items()
            })

        return params


class Feeds(MWS):
    """
    Amazon MWS Feeds API
    """

    ACCOUNT_TYPE = "Merchant"
    NEXT_TOKEN_OPERATIONS = [
        'GetFeedSubmissionList',
    ]


    def submit_feed(self, feed, feed_type, marketplaceids=None,
                    content_type="text/xml", purge='false'):
        """
        Uploads a feed ( xml or .tsv ) to the seller's inventory.
        Can be used for creating/updating products on Amazon.
        """
        data = dict(
            Action='SubmitFeed',
            FeedType=feed_type,
            PurgeAndReplace=purge
        )
        data.update(self.enumerate_params({
            'MarketplaceIdList.Id.': marketplaceids,
        }))
        md5_hash = calc_md5(feed)
        return self.make_request(
            data,
            method="POST",
            body=feed,
            extra_headers={
                'Content-MD5': md5_hash, 'Content-Type': content_type
            }
        )


    def get_feed_submission_list(self, feedids=None, max_count=None,
                                 feedtypes=None, processingstatuses=None,
                                 fromdate=None, todate=None):
        """
        Returns a list of all feed submissions submitted in the
        previous 90 days that match the query parameters.
        """

        data = dict(
            Action='GetFeedSubmissionList',
            MaxCount=max_count,
            SubmittedFromDate=fromdate,
            SubmittedToDate=todate,
        )
        data.update(self.enumerate_params({
            'FeedSubmissionIdList.Id': feedids,
            'FeedTypeList.Type.': feedtypes,
            'FeedProcessingStatusList.Status.': processingstatuses,
        }))
        return self.make_request(data)


    def get_submission_list_by_next_token(self, token):
        print(self.ACTION_BY_NEXT_TOKEN_DEPRECATION_WARNING)
        data = dict(Action='GetFeedSubmissionListByNextToken', NextToken=token)
        return self.make_request(data)


    def get_feed_submission_count(self, feedtypes=None,
                                  processingstatuses=None, fromdate=None,
                                  todate=None):
        """
        Returns a count of the feeds submitted in the previous 90 days.
        """
        data = dict(
            Action='GetFeedSubmissionCount',
            SubmittedFromDate=fromdate,
            SubmittedToDate=todate
        )
        data.update(self.enumerate_params({
            'FeedTypeList.Type.': feedtypes,
            'FeedProcessingStatusList.Status.': processingstatuses,
        }))
        return self.make_request(data)


    def cancel_feed_submissions(self, feedids=None, feedtypes=None,
                                fromdate=None, todate=None):
        """
        Cancels one or more feed submissions and returns a count
        of the feed submissions that were canceled.
        """
        data = dict(
            Action='CancelFeedSubmissions',
            SubmittedFromDate=fromdate,
            SubmittedToDate=todate
        )
        data.update(self.enumerate_params({
            'FeedSubmissionIdList.Id.': feedids,
            'FeedTypeList.Type.': feedtypes,
        }))
        return self.make_request(data)


    def get_feed_submission_result(self, feedid):
        """
        Returns the feed processing report and the Content-MD5 header.
        """
        data = dict(
            Action='GetFeedSubmissionResult',
            FeedSubmissionId=feedid
        )
        return self.make_request(data)


class Reports(MWS):
    """
    Amazon MWS Reports API
    """

    ACCOUNT_TYPE = "Merchant"
    NEXT_TOKEN_OPERATIONS = [
        'GetReportRequestList',
        'GetReportScheduleList',
    ]


    ## REPORTS ###


    def get_report(self, report_id):
        """
        Returns the contents of a report and the Content-MD5 header
        for the returned report body.
        """
        data = dict(
            Action='GetReport',
            ReportId=report_id
        )
        return self.make_request(data)


    def get_report_count(self, report_types=(), acknowledged=None,
                         fromdate=None, todate=None):
        """
        Returns a count of the reports, created in the previous 90 days,
        with a status of _DONE_ and that are available for download.
        """
        data = dict(
            Action='GetReportCount',
            Acknowledged=acknowledged,
            AvailableFromDate=fromdate,
            AvailableToDate=todate
        )
        data.update(self.enumerate_params({
            'ReportTypeList.Type.': report_types,
        }))
        return self.make_request(data)


    def get_report_list(self, requestids=(), max_count=None, types=(),
                        acknowledged=None, fromdate=None, todate=None):
        """
        Returns a list of reports that were created in the
        previous 90 days.
        """
        data = dict(
            Action='GetReportList',
            Acknowledged=acknowledged,
            AvailableFromDate=fromdate,
            AvailableToDate=todate,
            MaxCount=max_count
        )
        data.update(self.enumerate_params({
            'ReportRequestIdList.Id.': requestids,
            'ReportTypeList.Type.': types,
        }))
        return self.make_request(data)


    def get_report_list_by_next_token(self, token):
        print(self.ACTION_BY_NEXT_TOKEN_DEPRECATION_WARNING)
        data = dict(Action='GetReportListByNextToken', NextToken=token)
        return self.make_request(data)


    def get_report_request_count(self, report_types=(), processingstatuses=(),
                                 from_date=None, to_date=None):
        from_date = dt_iso_or_none(from_date)
        to_date = dt_iso_or_none(to_date)

        data = dict(
            Action='GetReportRequestCount',
            RequestedFromDate=from_date,
            RequestedToDate=to_date
        )
        data.update(self.enumerate_params({
            'ReportTypeList.Type.': report_types,
            'ReportProcessingStatusList.Status.': processingstatuses,
        }))
        return self.make_request(data)


    def get_report_request_list(self, requestids=(), types=(),
                                processingstatuses=(), max_count=None,
                                from_date=None, to_date=None):
        from_date = dt_iso_or_none(from_date)
        to_date = dt_iso_or_none(to_date)

        data = dict(
            Action='GetReportRequestList',
            MaxCount=max_count,
            RequestedFromDate=from_date,
            RequestedToDate=to_date
        )
        data.update(self.enumerate_params({
            'ReportRequestIdList.Id.': requestids,
            'ReportTypeList.Type.': types,
            'ReportProcessingStatusList.Status.': processingstatuses,
        }))
        return self.make_request(data)


    def get_report_request_list_by_next_token(self, token):
        print(self.ACTION_BY_NEXT_TOKEN_DEPRECATION_WARNING)
        data = dict(Action='GetReportRequestListByNextToken', NextToken=token)
        return self.make_request(data)


    def request_report(self, report_type,
                       start_date=None, end_date=None,
                       marketplaceids=()):
        data = dict(
            Action='RequestReport',
            ReportType=report_type,
            StartDate=start_date,
            EndDate=end_date
        )
        data.update(self.enumerate_params({
            'MarketplaceIdList.Id.': marketplaceids,
        }))
        return self.make_request(data)


    ### ReportSchedule ###


    def get_report_schedule_list(self, types=()):
        data = dict(
            Action='GetReportScheduleList'
        )
        data.update(self.enumerate_params({
            'ReportTypeList.Type.': types,
        }))
        return self.make_request(data)


    def get_report_schedule_count(self, types=()):
        data = dict(
            Action='GetReportScheduleCount'
        )
        data.update(self.enumerate_params({
            'ReportTypeList.Type.': types,
        }))
        return self.make_request(data)


class Orders(MWS):
    """
    Amazon Orders API
    """

    URI = "/Orders/2011-01-01"
    VERSION = "2011-01-01"
    NAMESPACE = '{https://mws.amazonservices.com/Orders/2011-01-01}'
    NEXT_TOKEN_OPERATIONS = [
        'ListOrders',
        'ListOrderItems',
    ]


    def list_orders(self, marketplaceids, created_after=None,
                    created_before=None, last_updated_after=None,
                    last_updated_before=None, orderstatus=(),
                    fulfillment_channels=(), payment_methods=(),
                    buyer_email=None, seller_orderid=None,
                    max_results='100'):
        """
        Returns orders created or updated during a
        time frame that you specify.
        """
        created_after = dt_iso_or_none(created_after)
        created_before = dt_iso_or_none(created_before)
        last_updated_after = dt_iso_or_none(last_updated_after)
        last_updated_before = dt_iso_or_none(last_updated_before)

        data = dict(
            Action='ListOrders',
            CreatedAfter=created_after,
            CreatedBefore=created_before,
            LastUpdatedAfter=last_updated_after,
            LastUpdatedBefore=last_updated_before,
            BuyerEmail=buyer_email,
            SellerOrderId=seller_orderid,
            MaxResultsPerPage=max_results,
        )
        data.update(self.enumerate_params({
            'OrderStatus.Status.': orderstatus,
            'MarketplaceId.Id.': marketplaceids,
            'FulfillmentChannel.Channel.': fulfillment_channels,
            'PaymentMethod.Method.': payment_methods,
        }))
        return self.make_request(data)


    def list_orders_by_next_token(self, token):
        print(self.ACTION_BY_NEXT_TOKEN_DEPRECATION_WARNING)
        data = dict(Action='ListOrdersByNextToken', NextToken=token)
        return self.make_request(data)


    def get_order(self, amazon_order_ids):
        """
        Returns orders based on the AmazonOrderId values that you specify.
        """
        data = dict(
            Action='GetOrder'
        )
        data.update(self.enumerate_params({
            'AmazonOrderId.Id.': amazon_order_ids
        }))
        return self.make_request(data)


    def list_order_items(self, amazon_order_id):
        """
        Returns order items based on the AmazonOrderId that you specify.
        """
        data = dict(
            Action='ListOrderItems',
            AmazonOrderId=amazon_order_id
        )
        return self.make_request(data)


    def list_order_items_by_next_token(self, token):
        print(self.ACTION_BY_NEXT_TOKEN_DEPRECATION_WARNING)
        data = dict(Action='ListOrderItemsByNextToken', NextToken=token)
        return self.make_request(data)


class Products(MWS):
    """
    Amazon MWS Products API
    """

    URI = '/Products/2011-10-01'
    VERSION = '2011-10-01'
    NAMESPACE = '{http://mws.amazonservices.com/schema/Products/2011-10-01}'
    NEXT_TOKEN_OPERATIONS = []


    def list_matching_products(self, marketplaceid, query, contextid=None):
        """
        Returns a list of products and their attributes, ordered by
        relevancy, based on a search query that you specify.

        Your search query can be a phrase that describes the product
        or it can be a product identifier such as a UPC, EAN, ISBN, or JAN.
        """
        data = dict(
            Action='ListMatchingProducts',
            MarketplaceId=marketplaceid,
            Query=query,
            QueryContextId=contextid
        )
        return self.make_request(data)


    def get_matching_product(self, marketplaceid, asins):
        """
        Returns a list of products and their attributes, based on a list of
        ASIN values that you specify.
        """
        data = dict(
            Action='GetMatchingProduct',
            MarketplaceId=marketplaceid
        )
        data.update(self.enumerate_params({
            'ASINList.ASIN.': asins,
        }))
        return self.make_request(data)


    def get_matching_product_for_id(self, marketplaceid, identifier_type, ids):
        """
        Returns a list of products and their attributes, based on a list of
        product identifier values
        (ASIN, SellerSKU, UPC, EAN, ISBN, GCID  and JAN)
        The identifier type is case sensitive.
        Added in Fourth Release, API version 2011-10-01
        """
        data = dict(
            Action='GetMatchingProductForId',
            MarketplaceId=marketplaceid,
            IdType=identifier_type
        )
        data.update(self.enumerate_params({
            'IdList.Id.': ids,
        }))
        return self.make_request(data)


    def get_competitive_pricing_for_sku(self, marketplaceid, skus):
        """
        Returns the current competitive pricing of a product,
        based on the SellerSKU and MarketplaceId that you specify.
        """
        data = dict(
            Action='GetCompetitivePricingForSKU',
            MarketplaceId=marketplaceid
        )
        data.update(self.enumerate_params({
            'SellerSKUList.SellerSKU.': skus,
        }))
        return self.make_request(data)


    def get_competitive_pricing_for_asin(self, marketplaceid, asins):
        """
        Returns the current competitive pricing of a product,
        based on the ASIN and MarketplaceId that you specify.
        """
        data = dict(
            Action='GetCompetitivePricingForASIN',
            MarketplaceId=marketplaceid
        )
        data.update(self.enumerate_params({
            'ASINList.ASIN.': asins,
        }))
        return self.make_request(data)


    def get_lowest_offer_listings_for_sku(self, marketplaceid, skus,
                                          condition="Any", excludeme="False"):
        """
        Returns pricing information for the lowest-price active
        offer listings for a product, based on SellerSKU.
        """
        data = dict(
            Action='GetLowestOfferListingsForSKU',
            MarketplaceId=marketplaceid,
            ItemCondition=condition,
            ExcludeMe=excludeme
        )
        data.update(self.enumerate_params({
            'SellerSKUList.SellerSKU.', skus
        }))
        return self.make_request(data)


    def get_lowest_offer_listings_for_asin(self, marketplaceid, asins,
                                           condition="Any", excludeme="False"):
        """
        Returns pricing information for the lowest-price active
        offer listings for a product, based on ASIN.
        """
        data = dict(
            Action='GetLowestOfferListingsForASIN',
            MarketplaceId=marketplaceid,
            ItemCondition=condition,
            ExcludeMe=excludeme
        )
        data.update(self.enumerate_params({
            'ASINList.ASIN.': asins,
        }))
        return self.make_request(data)


    def get_lowest_priced_offers_for_sku(self, marketplaceid, sku,
                                         condition="New", excludeme="False"):
        data = dict(Action='GetLowestPricedOffersForSKU',
                    MarketplaceId=marketplaceid,
                    SellerSKU=sku,
                    ItemCondition=condition,
                    ExcludeMe=excludeme)
        return self.make_request(data)

    def get_lowest_priced_offers_for_asin(self, marketplaceid, asin,
                                          condition="New", excludeme="False"):
        data = dict(Action='GetLowestPricedOffersForASIN',
                    MarketplaceId=marketplaceid,
                    ASIN=asin,
                    ItemCondition=condition,
                    ExcludeMe=excludeme)
        return self.make_request(data)


    def get_product_categories_for_sku(self, marketplaceid, sku):
        data = dict(
            Action='GetProductCategoriesForSKU',
            MarketplaceId=marketplaceid,
            SellerSKU=sku
        )
        return self.make_request(data)


    def get_product_categories_for_asin(self, marketplaceid, asin):
        data = dict(
            Action='GetProductCategoriesForASIN',
            MarketplaceId=marketplaceid,
            ASIN=asin
        )
        return self.make_request(data)


    def get_my_price_for_sku(self, marketplaceid, skus, condition=None):
        data = dict(
            Action='GetMyPriceForSKU',
            MarketplaceId=marketplaceid,
            ItemCondition=condition
        )
        data.update(self.enumerate_params({
            'SellerSKUList.SellerSKU.': skus,
        }))
        return self.make_request(data)


    def get_my_price_for_asin(self, marketplaceid, asins, condition=None):
        data = dict(
            Action='GetMyPriceForASIN',
            MarketplaceId=marketplaceid,
            ItemCondition=condition
        )
        data.update(self.enumerate_params({
            'ASINList.ASIN.': asins,
        }))
        return self.make_request(data)


class Sellers(MWS):
    """
    Amazon MWS Sellers API
    """

    URI = '/Sellers/2011-07-01'
    VERSION = '2011-07-01'
    NAMESPACE = '{http://mws.amazonservices.com/schema/Sellers/2011-07-01}'
    NEXT_TOKEN_OPERATIONS = [
        'ListMarketplaceParticipations',
    ]


    def list_marketplace_participations(self):
        """
        Returns a list of marketplaces a seller can participate in and
        a list of participations that include seller-specific information
        in that marketplace. The operation returns only those marketplaces
        where the seller's account is in an active state.
        """
        data = dict(
            Action='ListMarketplaceParticipations'
        )
        return self.make_request(data)


    def list_marketplace_participations_by_next_token(self, token):
        """
        Takes a "NextToken" and returns the same information as "list_marketplace_participations".
        Based on the "NextToken".
        """
        print(self.ACTION_BY_NEXT_TOKEN_DEPRECATION_WARNING)
        data = dict(
            Action='ListMarketplaceParticipationsByNextToken',
            NextToken=token
        )
        return self.make_request(data)


#### Fulfillment APIs ####


class InboundShipments(MWS):
    """
    Amazon MWS FulfillmentInboundShipment API
    """
    URI = "/FulfillmentInboundShipment/2010-10-01"
    VERSION = '2010-10-01'
    NAMESPACE = '{http://mws.amazonaws.com/FulfillmentInboundShipment/2010-10-01/}'
    NEXT_TOKEN_OPERATIONS = [
        'ListInboundShipments',
        'ListInboundShipmentItems',
    ]
    SHIPMENT_STATUSES = ['WORKING', 'SHIPPED', 'CANCELLED']
    DEFAULT_SHIP_STATUS = 'WORKING'
    LABEL_PREFERENCES = ['SELLER_LABEL',
                         'AMAZON_LABEL_ONLY',
                         'AMAZON_LABEL_PREFERRED']


    def __init__(self, *args, **kwargs):
        """
        Allow the addition of a from_address dict during object initialization.
        kwarg "from_address" is caught and popped here,
        then calls set_ship_from_address.
        If empty or left out, empty dict is set by default.
        """
        self.from_address = {}
        addr = kwargs.pop('from_address', None)
        if addr is not None:
            self.from_address = self.set_ship_from_address(addr)
        super().__init__(*args, **kwargs)


    def set_ship_from_address(self, address):
        """
        Verifies the structure of an address dictionary.
        Once verified against the KEY_CONFIG, saves a parsed version
        of that dictionary, ready to send to requests.
        """
        # Clear existing
        self.from_address = None

        if not address:
            raise MWSError('Missing required `address` dict.')
        if not isinstance(address, dict):
            raise MWSError("`address` must be a dict")

        key_config = [
            # Sets composed of:
            # (input_key, output_key, is_required, default_value)
            ('name', 'Name', True, None),
            ('address_1', 'AddressLine1', True, None),
            ('address_2', 'AddressLine2', False, None),
            ('city', 'City', True, None),
            ('district_or_county', 'DistrictOrCounty', False, None),
            ('state_or_province', 'StateOrProvinceCode', False, None),
            ('postal_code', 'PostalCode', False, None),
            ('country', 'CountryCode', False, 'US'),
        ]

        # Check if all REQUIRED keys in address exist:
        if not all(k in address for k in
                   [c[0] for c in key_config if c[2]]):
            # Required parts of address missing
            raise MWSError((
                "`address` dict missing required keys: {required}."
                "\n- Optional keys: {optional}."
            ).format(
                required=", ".join([c[0] for c in key_config if c[2]]),
                optional=", ".join([c[0] for c in key_config if not c[2]]),
            ))

        # Passed tests. Assign values
        addr = {'ShipFromAddress.{}'.format(c[1]): address.get(c[0], c[3])
                for c in key_config}
        self.from_address = addr


    def _parse_item_args(self, item_args, operation):
        if not item_args:
            raise MWSError("One or more `item` dict arguments required.")

        # KEY_CONFIG to contain sets composed of:
        # (input_key, output_key, is_required, default_value)
        if operation == 'CreateInboundShipmentPlan':
            key_config = [
                ('sku', 'SellerSKU', True, None),
                ('quantity', 'Quantity', True, None),
                ('quantity_in_case', 'QuantityInCase', False, None),
                ('asin', 'ASIN', False, None),
                ('condition', 'Condition', False, None),
            ]
            quantity_key = 'Quantity'
        else:
            key_config = [
                ('sku', 'SellerSKU', True, None),
                ('quantity', 'QuantityShipped', True, None),
                ('quantity_in_case', 'QuantityInCase', False, None),
            ]
            quantity_key = 'QuantityShipped'

        items = []
        for item in item_args:
            if not isinstance(item, dict):
                raise MWSError("`item` argument must be a dict.")
            if not all(k in item for k in
                       [c[0] for c in key_config if c[2]]):
                # Required keys of an item line missing
                raise MWSError((
                    "`item` dict missing required keys: {required}."
                    "\n- Optional keys: {optional}."
                ).format(
                    required=', '.join([c[0] for c in key_config if c[2]]),
                    optional=', '.join([c[0] for c in key_config if not c[2]]),
                ))

            quantity = item.get('quantity')
            if quantity is not None:
                quantity = str(quantity)

            quantity_in_case = item.get('quantity_in_case')
            if quantity_in_case is not None:
                quantity_in_case = str(quantity_in_case)

            item_dict = {
                'SellerSKU': item.get('sku'),
                quantity_key: quantity,
                'QuantityInCase': quantity_in_case,
            }
            item_dict.update({
                c[1]: item.get(c[0], c[3])
                for c in key_config
                if c[0] not in ['sku', 'quantity', 'quantity_in_case']
            })
            items.append(item_dict)

        return items


    def create_inbound_shipment_plan(self, items, country_code='US',
                                     subdivision_code='', label_preference=''):
        """
        Returns one or more inbound shipment plans, which provide the
        information you need to create inbound shipments.

        At least one dictionary must be passed as `args`. Each dictionary
        should contain the following keys:
          REQUIRED: 'sku', 'quantity'
          OPTIONAL: 'asin', 'condition', 'quantity_in_case'

        'from_address' is required. Call 'set_ship_from_address' first before
        using this operation.
        """
        if not items:
            raise MWSError("One or more `item` dict arguments required.")
        subdivision_code = subdivision_code or None
        label_preference = label_preference or None

        items = self._parse_item_args(items, 'CreateInboundShipmentPlan')
        if not self.from_address:
            raise MWSError((
                "ShipFromAddress has not been set. "
                "Please use `.set_ship_from_address()` first."
            ))

        data = dict(
            Action='CreateInboundShipmentPlan',
            ShipToCountryCode=country_code,
            ShipToCountrySubdivisionCode=subdivision_code,
            LabelPrepPreference=label_preference,
        )
        data.update(self.from_address)
        data.update(self.enumerate_keyed_param(
            'InboundShipmentPlanRequestItems.member', items,
        ))
        return self.make_request(data, method="POST")


    def create_inbound_shipment(self, shipment_id, shipment_name,
                                destination, items, shipment_status='',
                                label_preference='', case_required=False,
                                box_contents_source=None):
        """
        Creates an inbound shipment to Amazon's fulfillment network.

        At least one dictionary must be passed as `items`. Each dictionary
        should contain the following keys:
          REQUIRED: 'sku', 'quantity'
          OPTIONAL: 'quantity_in_case'

        'from_address' is required. Call 'set_ship_from_address' first before
        using this operation.
        """
        assert isinstance(shipment_id, str), "`shipment_id` must be a string."
        assert isinstance(shipment_name, str), "`shipment_name` must be a string."
        assert isinstance(destination, str), "`destination` must be a string."

        if not items:
            raise MWSError("One or more `item` dict arguments required.")

        items = self._parse_item_args(items, 'CreateInboundShipment')

        if not self.from_address:
            raise MWSError((
                "ShipFromAddress has not been set. "
                "Please use `.set_ship_from_address()` first."
            ))
        from_address = self.from_address
        from_address = {'InboundShipmentHeader.{}'.format(k): v
                        for k, v in from_address.items()}

        if shipment_status not in self.SHIPMENT_STATUSES:
            # Status is required for `create` request.
            # Set it to default.
            shipment_status = self.DEFAULT_SHIP_STATUS

        if label_preference not in self.LABEL_PREFERENCES:
            # Label preference not required. Set to None
            label_preference = None

        # Explict True/False for case_required,
        # written as the strings MWS expects.
        case_required = 'true' if case_required else 'false'

        data = {
            'Action': 'CreateInboundShipment',
            'ShipmentId': shipment_id,
            'InboundShipmentHeader.ShipmentName': shipment_name,
            'InboundShipmentHeader.DestinationFulfillmentCenterId': destination,
            'InboundShipmentHeader.LabelPrepPreference': label_preference,
            'InboundShipmentHeader.AreCasesRequired': case_required,
            'InboundShipmentHeader.ShipmentStatus': shipment_status,
            'InboundShipmentHeader.IntendedBoxContentsSource': box_contents_source,
        }
        data.update(from_address)
        data.update(self.enumerate_keyed_param(
            'InboundShipmentItems.member', items,
        ))
        return self.make_request(data, method="POST")


    def update_inbound_shipment(self, shipment_id, shipment_name,
                                destination, items=None, shipment_status='',
                                label_preference='', case_required=False,
                                box_contents_source=None):
        """
        Updates an existing inbound shipment in Amazon FBA.
        'from_address' is required. Call 'set_ship_from_address' first before
        using this operation.
        """
        # Assert these are strings, error out if not.
        assert isinstance(shipment_id, str), "`shipment_id` must be a string."
        assert isinstance(shipment_name, str), "`shipment_name` must be a string."
        assert isinstance(destination, str), "`destination` must be a string."

        # Parse item args
        if items:
            items = self._parse_item_args(items, 'UpdateInboundShipment')
        else:
            items = None

        # Raise exception if no from_address has been set prior to calling
        if not self.from_address:
            raise MWSError((
                "ShipFromAddress has not been set. "
                "Please use `.set_ship_from_address()` first."
            ))
        # Assemble the from_address using operation-specific header
        from_address = self.from_address
        from_address = {'InboundShipmentHeader.{}'.format(k): v
                        for k, v in from_address.items()}

        if shipment_status not in self.SHIPMENT_STATUSES:
            # Passed shipment status is an invalid choice.
            # Remove it from this request by setting it to None.
            shipment_status = None

        if label_preference not in self.LABEL_PREFERENCES:
            # Passed label preference is an invalid choice.
            # Remove it from this request by setting it to None.
            label_preference = None

        case_required = 'true' if case_required else 'false'

        data = {
            'Action': 'UpdateInboundShipment',
            'ShipmentId': shipment_id,
            'InboundShipmentHeader.ShipmentName': shipment_name,
            'InboundShipmentHeader.DestinationFulfillmentCenterId': destination,
            'InboundShipmentHeader.LabelPrepPreference': label_preference,
            'InboundShipmentHeader.AreCasesRequired': case_required,
            'InboundShipmentHeader.ShipmentStatus': shipment_status,
            'InboundShipmentHeader.IntendedBoxContentsSource': box_contents_source,
        }
        data.update(from_address)
        if items:
            # Update with an items paramater only if they exist.
            data.update(self.enumerate_keyed_param(
                'InboundShipmentItems.member', items,
            ))
        return self.make_request(data, method="POST")


    def get_prep_instructions_for_sku(self, skus=None, country_code=None):
        """
        Returns labeling requirements and item preparation instructions
        to help you prepare items for an inbound shipment.
        """
        country_code = country_code or 'US'
        skus = skus or []

        # 'skus' should be a unique list, or there may be an error returned.
        skus = unique_list_order_preserved(skus)

        data = dict(
            Action='GetPrepInstructionsForSKU',
            ShipToCountryCode=country_code,
        )
        data.update(self.enumerate_params({
            'SellerSKUList.ID.': skus,
        }))
        return self.make_request(data, method="POST")


    def get_prep_instructions_for_asin(self, asins=None, country_code=None):
        """
        Returns item preparation instructions to help with
        item sourcing decisions.
        """
        country_code = country_code or 'US'
        asins = asins or []

        # 'asins' should be a unique list, or there may be an error returned.
        asins = unique_list_order_preserved(asins)

        data = dict(
            Action='GetPrepInstructionsForASIN',
            ShipToCountryCode=country_code,
        )
        data.update(self.enumerate_params({
            'ASINList.ID.': asins,
        }))
        return self.make_request(data, method="POST")


    def get_package_labels(self, shipment_id, num_packages, page_type=None):
        """
        Returns PDF document data for printing package labels for
        an inbound shipment.
        """
        data = dict(
            Action='GetPackageLabels',
            ShipmentId=shipment_id,
            PageType=page_type,
            NumberOfPackages=str(num_packages),
        )
        return self.make_request(data, method="POST")


    def get_transport_content(self, shipment_id):
        """
        Returns current transportation information about an
        inbound shipment.
        """
        data = dict(
            Action='GetTransportContent',
            ShipmentId=shipment_id
        )
        return self.make_request(data, method="POST")


    def estimate_transport_request(self, shipment_id):
        """
        Requests an estimate of the shipping cost for an inbound shipment.
        """
        data = dict(
            Action='EstimateTransportRequest',
            ShipmentId=shipment_id,
        )
        return self.make_request(data, method="POST")


    def void_transport_request(self, shipment_id):
        """
        Voids a previously-confirmed request to ship your inbound shipment
        using an Amazon-partnered carrier.
        """
        data = dict(
            Action='VoidTransportRequest',
            ShipmentId=shipment_id
        )
        return self.make_request(data, method="POST")


    def get_bill_of_lading(self, shipment_id):
        """
        Returns PDF document data for printing a bill of lading
        for an inbound shipment.
        """
        data = dict(
            Action='GetBillOfLading',
            ShipmentId=shipment_id,
        )
        return self.make_request(data, "POST")


    def list_inbound_shipments(self, shipment_ids=None, shipment_statuses=None,
                               last_updated_after=None, last_updated_before=None,
                               next_token=None):
        """
        Returns list of shipments based on statuses, IDs, and/or
        before/after datetimes.
        """
        if next_token:
            return self.action_by_next_token(
                'ListInboundShipments',
                next_token
            )

        last_updated_after = dt_iso_or_none(last_updated_after)
        last_updated_before = dt_iso_or_none(last_updated_before)

        data = dict(
            Action='ListInboundShipments',
            LastUpdatedAfter=last_updated_after,
            LastUpdatedBefore=last_updated_before,
        )
        data.update(self.enumerate_params({
            'ShipmentStatusList.member.': shipment_statuses,
            'ShipmentIdList.member.': shipment_ids,
        }))
        return self.make_request(data, method="POST")


    def list_inbound_shipment_items(self, shipment_id=None,
                                    last_updated_after=None,
                                    last_updated_before=None,
                                    next_token=None):
        """
        Returns list of items within inbound shipments and/or
        before/after datetimes.
        """
        if next_token:
            return self.action_by_next_token(
                'ListInboundShipmentItems',
                next_token
            )

        last_updated_after = dt_iso_or_none(last_updated_after)
        last_updated_before = dt_iso_or_none(last_updated_before)

        data = dict(
            Action='ListInboundShipmentItems',
            ShipmentId=shipment_id,
            LastUpdatedAfter=last_updated_after,
            LastUpdatedBefore=last_updated_before,
        )
        return self.make_request(data, method="POST")


class Inventory(MWS):
    """
    Amazon MWS Inventory Fulfillment API
    """

    URI = '/FulfillmentInventory/2010-10-01'
    VERSION = '2010-10-01'
    NAMESPACE = "{http://mws.amazonaws.com/FulfillmentInventory/2010-10-01}"
    NEXT_TOKEN_OPERATIONS = [
        'ListInventorySupply',
    ]


    def list_inventory_supply(self, skus=(), start_time=None,
                              response_group='Basic'):
        """
        Returns information on available inventory
        """

        data = dict(Action='ListInventorySupply',
                    QueryStartDateTime=start_time,
                    ResponseGroup=response_group)
        data.update(self.enumerate_params({
            'SellerSkus.member.': skus,
        }))
        return self.make_request(data, method="POST")


    def list_inventory_supply_by_next_token(self, token):
        print(self.ACTION_BY_NEXT_TOKEN_DEPRECATION_WARNING)
        data = dict(Action='ListInventorySupplyByNextToken', NextToken=token)
        return self.make_request(data, method="POST")


class OutboundShipments(MWS):
    URI = "/FulfillmentOutboundShipment/2010-10-01"
    VERSION = "2010-10-01"
    NEXT_TOKEN_OPERATIONS = [
        'ListAllFulfillmentOrders',
    ]
    # To be completed


class Recommendations(MWS):

    """
    Amazon MWS Recommendations API
    """

    URI = '/Recommendations/2013-04-01'
    VERSION = '2013-04-01'
    NAMESPACE = "{https://mws.amazonservices.com/Recommendations/2013-04-01}"


    def get_last_updated_time_for_recommendations(self, marketplaceid):
        """
        Checks whether there are active recommendations for each
        category for the given marketplace, and if there are, returns
        the time when recommendations were last updated for each category.
        """

        data = dict(
            Action='GetLastUpdatedTimeForRecommendations',
            MarketplaceId=marketplaceid,
        )
        return self.make_request(data, "POST")


    def list_recommendations(self, marketplaceid, recommendationcategory=None):
        """
        Returns your active recommendations for a specific category or
        for all categories for a specific marketplace.
        """

        data = dict(
            Action="ListRecommendations",
            MarketplaceId=marketplaceid,
            RecommendationCategory=recommendationcategory
        )
        return self.make_request(data, "POST")


    def list_recommendations_by_next_token(self, token):
        """
        Returns the next page of recommendations using the NextToken parameter.
        """
        print(self.ACTION_BY_NEXT_TOKEN_DEPRECATION_WARNING)
        data = dict(Action="ListRecommendationsByNextToken",
                    NextToken=token)
        return self.make_request(data, "POST")
