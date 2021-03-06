#!/usr/bin/env python

import unittest
import json
import dateutil.parser
import uuid
import copy
from app import create_app, db
from app.auth import current_identity
from app.auth.identity import Identity
from app.utils import HostWrapper
from base64 import b64encode
from json import dumps
from datetime import datetime, timezone
from urllib.parse import urlsplit, urlencode, parse_qs, urlunsplit

HOST_URL = "/r/insights/platform/inventory/api/v1/hosts"
HEALTH_URL = "/health"
METRICS_URL = "/metrics"

NS = "testns"
ID = "whoabuddy"

ACCOUNT = "000031"
FACTS = [{"namespace": "ns1", "facts": {"key1": "value1"}}]
TAGS = ["aws/new_tag_1:new_value_1", "aws/k:v"]
ACCOUNT = "000501"


def test_data(display_name="hi", tags=None, facts=None):
    return {
        "account": ACCOUNT,
        "display_name": display_name,
        # "insights_id": "1234-56-789",
        # "rhel_machine_id": "1234-56-789",
        # "ip_addresses": ["10.10.0.1", "10.0.0.2"],
        "ip_addresses": ["10.10.0.1"],
        # "mac_addresses": ["c2:00:d0:c8:61:01"],
        "tags": tags if tags else [],
        "facts": facts if facts else FACTS,
    }


def inject_qs(url, **kwargs):
    scheme, netloc, path, query, fragment = urlsplit(url)
    params = parse_qs(query)
    params.update(kwargs)
    new_query = urlencode(params, doseq=True)
    return urlunsplit((scheme, netloc, path, new_query, fragment))


class BaseAPITestCase(unittest.TestCase):
    def _get_valid_auth_header(self):
        identity = Identity(account_number=ACCOUNT)
        dict_ = {"identity": identity._asdict()}
        json_doc = json.dumps(dict_)
        auth_header = {"x-rh-identity": b64encode(json_doc.encode())}
        return auth_header

    def setUp(self):
        """
        Creates the application and a test client to make requests.
        """
        self.app = create_app(config_name="testing")
        self.client = self.app.test_client

    def get(self, path, status=200, return_response_as_json=True):
        return self._response_check(
            self.client().get(path, headers=self._get_valid_auth_header()),
            status,
            return_response_as_json,
        )

    def post(self, path, data, status=200, return_response_as_json=True):
        return self._make_http_call(
            self.client().post, path, data, status, return_response_as_json
        )

    def patch(self, path, data, status=200, return_response_as_json=True):
        return self._make_http_call(
            self.client().patch, path, data, status, return_response_as_json
        )

    def put(self, path, data, status=200, return_response_as_json=True):
        return self._make_http_call(
            self.client().put, path, data, status, return_response_as_json
        )

    def _make_http_call(
        self, http_method, path, data, status, return_response_as_json=True
    ):
        json_data = json.dumps(data)
        headers = self._get_valid_auth_header()
        headers["content-type"] = "application/json"
        return self._response_check(
            http_method(path, data=json_data, headers=headers),
            status,
            return_response_as_json,
        )

    def _response_check(self, response, status, return_response_as_json):
        self.assertEqual(response.status_code, status)
        if return_response_as_json:
            return json.loads(response.data)

        else:
            return response


class DBAPITestCase(BaseAPITestCase):

    @classmethod
    def setUpClass(cls):
        """
        Temporarily rename the host table while the tests run.  This is done
        to make dropping the table at the end of the tests a bit safer.
        """
        from app.models import Host
        temp_table_name_suffix = "__unit_tests__"
        if temp_table_name_suffix not in Host.__table__.name:
            Host.__table__.name = Host.__table__.name + temp_table_name_suffix
        if temp_table_name_suffix not in Host.__table__.fullname:
            Host.__table__.fullname = Host.__table__.fullname + temp_table_name_suffix

    def setUp(self):
        """
        Initializes the database by creating all tables.
        """
        super(DBAPITestCase, self).setUp()

        # binds the app to the current context
        with self.app.app_context():
            # create all tables
            db.create_all()

    def tearDown(self):
        """
        Cleans up the database by dropping all tables.
        """
        with self.app.app_context():
            # drop all tables
            db.session.remove()
            db.drop_all()

    def _build_host_id_list_for_url(self, host_list):
        host_id_list = [str(h.id) for h in host_list]

        return ",".join(host_id_list)


class CreateHostsTestCase(DBAPITestCase):
    def test_create_and_update(self):
        facts = None
        tags = ["/merge_me_1:value1"]

        host_data = HostWrapper(test_data(facts=facts, tags=tags))

        # initial create
        results = self.post(HOST_URL, host_data.data(), 201)

        self.assertIsNotNone(results["id"])
        self.assertIsNotNone(results["created"])
        created_time = dateutil.parser.parse(results["created"])
        self.assertGreater(datetime.now(timezone.utc), created_time)

        original_id = results["id"]

        post_data = host_data
        post_data.facts = FACTS

        # Replace facts under the first namespace
        post_data.facts[0]["facts"] = {"newkey1": "newvalue1"}

        # Add a new set of facts under a new namespace
        post_data.facts.append({"namespace": "ns2", "facts": {"key2": "value2"}})

        # Add a new canonical fact
        post_data.rhel_machine_id = "1234-56-789"
        post_data.ip_addresses = ["10.10.0.1", "10.0.0.2"]
        post_data.mac_addresses = ["c2:00:d0:c8:61:01"]

        # Add a new tag
        post_data.tags = ["aws/new_tag_1:new_value_1"]

        expected_tags = []

        # update initial entity
        results = self.post(HOST_URL, post_data.data(), 200)

        # make sure the id from the update post matches the id from the create
        self.assertEqual(results["id"], original_id)

        self.assertIsNotNone(results["updated"])
        modified_time = dateutil.parser.parse(results["updated"])
        self.assertGreater(modified_time, created_time)

        data = self.get("%s/%s" % (HOST_URL, original_id), 200)
        results = HostWrapper(data["results"][0])

        # sanity check
        # post_data.facts[0]["facts"]["key2"] = "blah"
        self.assertListEqual(results.facts, post_data.facts)

        # make sure the canonical facts are merged
        self.assertEqual(results.rhel_machine_id, post_data.rhel_machine_id)
        self.assertListEqual(results.ip_addresses, post_data.ip_addresses)
        self.assertListEqual(results.mac_addresses, post_data.mac_addresses)

        # make sure the tags are merged
        self.assertListEqual(results.tags, expected_tags)

    def test_create_host_with_empty_tags_facts_display_name_then_update(self):
        # Create a host with empty tags, facts, and display_name
        # then update those fields
        host_data = HostWrapper(test_data(facts=None))
        del host_data.tags
        del host_data.display_name
        del host_data.facts

        expected_tags = []

        # initial create
        results = self.post(HOST_URL, host_data.data(), 201)

        self.assertIsNotNone(results["id"])

        original_id = results["id"]

        host_data.tags = ["aws/new_tag_1:new_value_1", "aws/k:v"]
        host_data.facts = FACTS
        host_data.display_name = "expected_display_name"

        self.post(HOST_URL, host_data.data(), 200)

        data = self.get("%s/%s" % (HOST_URL, original_id), 200)
        results = HostWrapper(data["results"][0])

        self.assertListEqual(results.tags, expected_tags)

        self.assertListEqual(results.facts, host_data.facts)

        self.assertEqual(results.display_name, host_data.display_name)

    def test_create_host_with_display_name_as_None(self):
        host_data = HostWrapper(test_data(facts=None))

        # Explicitly set the display name to None
        host_data.display_name = None

        # initial create
        results = self.post(HOST_URL, host_data.data(), 201)

        self.assertIsNotNone(results["id"])

        self.assertIsNone(results["display_name"])

    def test_create_host_without_canonical_facts(self):
        host_data = HostWrapper(test_data(facts=None))
        del host_data.insights_id
        del host_data.rhel_machine_id
        del host_data.subscription_manager_id
        del host_data.satellite_id
        del host_data.bios_uuid
        del host_data.ip_addresses
        del host_data.fqdn
        del host_data.mac_addresses

        # FIXME: Verify response?
        response_data = self.post(HOST_URL, host_data.data(), 400)

    def test_create_host_without_account(self):
        host_data = HostWrapper(test_data(facts=None))
        del host_data.account

        # FIXME: Verify response?
        response_data = self.post(HOST_URL, host_data.data(), 400)

    def test_create_host_with_mismatched_account_numbers(self):
        host_data = HostWrapper(test_data(facts=None))
        host_data.account = ACCOUNT[::-1]

        # FIXME: Verify response?
        response_data = self.post(HOST_URL, host_data.data(), 400)

    def test_create_host_with_invalid_facts_no_namespace(self):
        facts = copy.deepcopy(FACTS)
        del facts[0]["namespace"]
        host_data = HostWrapper(test_data(facts=facts))

        response_data = self.post(HOST_URL, host_data.data(), 400)

        assert response_data["title"] == "Invalid request"
        assert "status" in response_data
        assert "detail" in response_data
        assert "type" in response_data

    def test_create_host_with_invalid_facts_no_facts(self):
        facts = copy.deepcopy(FACTS)
        del facts[0]["facts"]
        host_data = HostWrapper(test_data(facts=facts))

        response_data = self.post(HOST_URL, host_data.data(), 400)

        assert response_data["title"] == "Invalid request"
        assert "status" in response_data
        assert "detail" in response_data
        assert "type" in response_data


class PreCreatedHostsBaseTestCase(DBAPITestCase):
    def setUp(self):
        super(PreCreatedHostsBaseTestCase, self).setUp()
        self.added_hosts = self.create_hosts()

    def create_hosts(self):
        hosts_to_create = [("host1", "12345"), ("host2", "54321")]
        host_list = []

        for host in hosts_to_create:
            host_wrapper = HostWrapper()
            host_wrapper.account = ACCOUNT
            host_wrapper.tags = TAGS
            host_wrapper.display_name = host[0]
            host_wrapper.insights_id = host[1]
            host_wrapper.facts = [{"namespace": "ns1", "facts": {"key1": "value1"}}]

            response_data = self.post(HOST_URL, host_wrapper.data(), 201)
            host_list.append(HostWrapper(response_data))

        return host_list

    def _base_paging_test(self, url):
        test_url = inject_qs(url, page="1", per_page="1")
        response = self.get(test_url, 200)

        self.assertEqual(len(response["results"]), 1)
        self.assertEqual(response["count"], 1)
        self.assertEqual(response["total"], 2)

        test_url = inject_qs(url, page="2", per_page="1")
        response = self.get(test_url, 200)

        self.assertEqual(len(response["results"]), 1)
        self.assertEqual(response["count"], 1)
        self.assertEqual(response["total"], 2)

        test_url = inject_qs(url, page="3", per_page="1")
        response = self.get(test_url, 404)


class QueryTestCase(PreCreatedHostsBaseTestCase):
    def test_query_all(self):
        response = self.get(HOST_URL, 200)

        # FIXME: check the results
        self.assertEqual(len(response["results"]), len(self.added_hosts))

        self._base_paging_test(HOST_URL)

    def test_query_all_with_invalid_paging_parameters(self):
        invalid_limit_parameters = ["-1", "0", "notanumber"]
        for invalid_parameter in invalid_limit_parameters:
            self.get(HOST_URL + "?per_page=" + invalid_parameter, 400)

            self.get(HOST_URL + "?page=" + invalid_parameter, 400)

    def test_query_using_host_id_list(self):
        host_list = self.added_hosts

        url_host_id_list = self._build_host_id_list_for_url(host_list)

        test_url = HOST_URL + "/" + url_host_id_list

        response = self.get(test_url, 200)

        # FIXME: check the results
        self.assertEqual(len(response["results"]), 2)

        self._base_paging_test(test_url)

    def test_query_using_host_id_list_with_invalid_paging_parameters(self):
        host_list = self.added_hosts

        url_host_id_list = self._build_host_id_list_for_url(host_list)
        base_url = HOST_URL + "/" + url_host_id_list

        invalid_limit_parameters = ["-1", "0", "notanumber"]
        for invalid_parameter in invalid_limit_parameters:
            self.get(base_url + "?per_page=" + invalid_parameter, 400)

            self.get(base_url + "?page=" + invalid_parameter, 400)

    def test_query_using_host_id_list_include_nonexistent_host_ids(self):
        host_list = self.added_hosts

        url_host_id_list = self._build_host_id_list_for_url(host_list)

        # Add some host ids to the list that do not exist
        url_host_id_list = (
            url_host_id_list + "," + str(uuid.uuid4()) + "," + str(uuid.uuid4())
        )

        response = self.get(HOST_URL + "/" + url_host_id_list, 200)

        # FIXME: check the results
        self.assertEqual(len(response["results"]), 2)

    def test_query_using_display_name(self):
        host_list = self.added_hosts

        response = self.get(HOST_URL + "?display_name=" + host_list[0].display_name)

        # FIXME: check the results
        self.assertEqual(len(response["results"]), 1)

    def test_query_using_display_name_substring(self):
        host_list = self.added_hosts

        host_name_substr = host_list[0].display_name[:-2]

        test_url = HOST_URL + "?display_name=" + host_name_substr

        response = self.get(test_url)

        # FIXME: check the results
        self.assertEqual(len(response["results"]), 2)

        self._base_paging_test(test_url)


class FactsTestCase(PreCreatedHostsBaseTestCase):
    def _valid_fact_doc(self):
        return {"newfact1": "newvalue1", "newfact2": "newvalue2"}

    def _build_facts_url(self, host_list, namespace):
        if type(host_list) == list:
            url_host_id_list = self._build_host_id_list_for_url(host_list)
        else:
            url_host_id_list = str(host_list)
        return HOST_URL + "/" + url_host_id_list + "/facts/" + namespace

    def _basic_fact_test(self, input_facts, expected_facts, replace_facts):

        host_list = self.added_hosts

        # This test assumes the set of facts are the same across
        # the hosts in the host_list

        target_namespace = host_list[0].facts[0]["namespace"]

        url_host_id_list = self._build_host_id_list_for_url(host_list)

        patch_url = self._build_facts_url(host_list, target_namespace)

        if replace_facts:
            response = self.put(patch_url, input_facts, 200)
        else:
            response = self.patch(patch_url, input_facts, 200)

        response = self.get(f"{HOST_URL}/{url_host_id_list}", 200)

        self.assertEqual(len(response["results"]), len(host_list))

        for response_host in response["results"]:
            host_to_verify = HostWrapper(response_host)

            self.assertEqual(host_to_verify.facts[0]["facts"], expected_facts)

            self.assertEqual(host_to_verify.facts[0]["namespace"], target_namespace)

    def test_add_facts_without_fact_dict(self):
        patch_url = self._build_facts_url(1, "ns1")
        response = self.patch(patch_url, None, 400)
        self.assertEqual(response["detail"], "Request body is not valid JSON")

    def test_add_facts_to_multiple_hosts(self):
        facts_to_add = self._valid_fact_doc()

        host_list = self.added_hosts

        # This test assumes the set of facts are the same across
        # the hosts in the host_list

        expected_facts = {**host_list[0].facts[0]["facts"], **facts_to_add}

        self._basic_fact_test(facts_to_add, expected_facts, False)

    def test_replace_and_add_facts_to_multiple_hosts_including_nonexistent_host(self):
        facts_to_add = self._valid_fact_doc()

        host_list = self.added_hosts

        target_namespace = host_list[0].facts[0]["namespace"]

        url_host_id_list = self._build_host_id_list_for_url(host_list)

        # Add a couple of host ids that should not exist in the database
        url_host_id_list = (
            url_host_id_list + "," + str(uuid.uuid4()) + "," + str(uuid.uuid4())
        )

        patch_url = HOST_URL + "/" + url_host_id_list + "/facts/" + target_namespace

        # Add facts
        self.patch(patch_url, facts_to_add, 400)

        # Replace facts
        self.put(patch_url, facts_to_add, 400)

    def test_add_facts_to_multiple_hosts_overwrite_empty_key_value_pair(self):
        new_facts = {}
        expected_facts = new_facts

        # Set the value in the namespace to an empty fact set
        self._basic_fact_test(new_facts, expected_facts, True)

        new_facts = self._valid_fact_doc()
        expected_facts = new_facts

        # Overwrite the empty fact set
        self._basic_fact_test(new_facts, expected_facts, False)

    def test_add_facts_to_multiple_hosts_add_empty_fact_set(self):
        new_facts = {}
        target_namespace = self.added_hosts[0].facts[0]["namespace"]
        valid_host_id = self.added_hosts[0].id

        test_url = self._build_facts_url(valid_host_id, target_namespace)

        # Test merging empty facts set
        self.patch(test_url, new_facts, 400)

    def test_replace_and_add_facts_to_namespace_that_does_not_exist(self):
        valid_host_id = self.added_hosts[0].id
        facts_to_add = self._valid_fact_doc()
        test_url = self._build_facts_url(valid_host_id, "imanonexistentnamespace")

        # Test replace
        self.put(test_url, facts_to_add, 400)

        # Test add/merge
        self.patch(test_url, facts_to_add, 400)

    def test_replace_facts_without_fact_dict(self):
        put_url = self._build_facts_url(1, "ns1")
        response = self.put(put_url, None, 400)
        self.assertEqual(response["detail"], "Request body is not valid JSON")

    def test_replace_facts_on_multiple_hosts(self):
        new_facts = self._valid_fact_doc()
        expected_facts = new_facts

        self._basic_fact_test(new_facts, expected_facts, True)

    def test_replace_facts_on_multiple_hosts_with_empty_fact_set(self):
        new_facts = {}
        expected_facts = new_facts

        self._basic_fact_test(new_facts, expected_facts, True)

    def test_replace_empty_facts_on_multiple_hosts(self):
        new_facts = {}
        expected_facts = new_facts

        self._basic_fact_test(new_facts, expected_facts, True)

        new_facts = self._valid_fact_doc()
        expected_facts = new_facts

        self._basic_fact_test(new_facts, expected_facts, True)


class AuthTestCase(DBAPITestCase):
    @staticmethod
    def _valid_identity():
        """
        Provides a valid Identity object.
        """
        return Identity(account_number="some account number")

    @staticmethod
    def _valid_payload():
        """
        Builds a valid HTTP header payload – Base64 encoded JSON string with valid data.
        """
        identity = __class__._valid_identity()
        dict_ = {"identity": identity._asdict()}
        json = dumps(dict_)
        return b64encode(json.encode())

    def _get_hosts(self, headers):
        """
        Issues a GET request to the /hosts URL, providing given headers.
        """
        return self.client().get(HOST_URL, headers=headers)

    def test_validate_missing_identity(self):
        """
        Identity header is not present, 400 Bad Request is returned.
        """
        response = self._get_hosts({})
        self.assertEqual(400, response.status_code)  # Bad Request

    def test_validate_invalid_identity(self):
        """
        Identity header is not valid – empty in this case, 403 Forbidden is returned.
        """
        response = self._get_hosts({"x-rh-identity": ""})
        self.assertEqual(403, response.status_code)  # Forbidden

    def test_validate_valid_identity(self):
        """
        Identity header is valid – non-empty in this case, 200 is returned.
        """
        payload = self._valid_payload()
        response = self._get_hosts({"x-rh-identity": payload})
        self.assertEqual(200, response.status_code)  # OK


class HealthTestCase(BaseAPITestCase):
    """
    Tests the health check endpoint.
    """

    def test_heath(self):
        """
        The health check simply returns 200 to any GET request. The response body is
        irrelevant.
        """
        response = self.client().get(HEALTH_URL)  # No identity header.
        self.assertEqual(200, response.status_code)

    def test_metrics(self):
        """
        The metrics endpoint simply returns 200 to any GET request.
        """
        response = self.client().get(METRICS_URL)  # No identity header.
        self.assertEqual(200, response.status_code)


if __name__ == "__main__":
    unittest.main()
