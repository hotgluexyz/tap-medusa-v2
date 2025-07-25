"""REST client handling, including MedusaStream base class."""

import datetime
import json
from typing import Any, Dict, Optional, Union

import requests
from memoization import cached
from pendulum import parse
from singer.schema import Schema
from singer_sdk.helpers.jsonpath import extract_jsonpath
from singer_sdk.plugin_base import PluginBase as TapBaseClass
from singer_sdk.streams import RESTStream

TOKEN_EXPIRY_BUFFER_SECONDS = 120
TOKEN_VALIDITY_MINUTES = 60


class MedusaStream(RESTStream):
    """Medusa stream class."""

    def __init__(
        self,
        tap: TapBaseClass,
        name: Optional[str] = None,
        schema: Optional[Union[Dict[str, Any], Schema]] = None,
        path: Optional[str] = None,
    ) -> None:
        super().__init__(tap, name=name, schema=schema, path=path)

    records_jsonpath = "$[*]"
    additional_params = {}

    @property
    def base_url(self):
        return self.config.get("base_url", "").rstrip("/")

    @property
    def url_base(self):
        return f"{self.base_url}/admin" if not self.base_url.endswith("/admin") else self.base_url

    @property
    def auth_url(self):
        return f"{self.base_url}/auth/user/emailpass"

    @property
    def http_headers(self) -> dict:
        """Return the http headers needed."""
        headers = {}
        if "user_agent" in self.config:
            headers["User-Agent"] = self.config.get("user_agent")
        # if api_key auth with api_key
        if self.config.get("api_key"):
            headers["x-medusa-access-token"] = self.config.get("api_key")
        else:
            # add authentication header
            headers["Authorization"] = f"Bearer {self.get_access_token()}"
        return headers
    
    def is_token_valid(self) -> bool:
        """Check if the current access token is still valid."""
        access_token = self._tap._config.get("access_token")
        now = round(datetime.datetime.utcnow().timestamp())
        expires_in = self._tap.config.get("expires_in")
        if expires_in is not None:
            expires_in = int(expires_in)
        if not access_token:
            return False
        if not expires_in:
            return False
        return not ((expires_in - now) < TOKEN_EXPIRY_BUFFER_SECONDS)

    def get_access_token(self):
        """Get a valid access token, refreshing if necessary."""
        current_token = self._tap._config.get("access_token")
        if not self.is_token_valid():
            headers = {"Content-Type": "application/json"}
            login_data = {
                "email": self.config.get("email"),
                "password": self.config.get("password"),
            }
            
            auth_response = requests.post(
                url=self.auth_url,
                data=json.dumps(login_data),
                headers=headers,
            )
            try:
                self.validate_response(auth_response)
            except Exception as e:
                raise Exception(f"Failed during token generation: {e}")
            
            new_token = auth_response.json()["token"]
            self._tap._config["access_token"] = new_token
            expiry_timestamp = round((datetime.datetime.utcnow() + datetime.timedelta(minutes=TOKEN_VALIDITY_MINUTES)).timestamp())
            self._tap._config["expires_in"] = expiry_timestamp

            # write access token in config file
            with open(self._tap.config_file, "w") as outfile:
                json.dump(self._tap._config, outfile, indent=4)
            
            return new_token

        return current_token

    def get_next_page_token(
        self, response: requests.Response, previous_token: Optional[Any]
    ) -> Optional[Any]:
        """Return a token for identifying next page or None if no more pages."""
        previous_token = previous_token or 0
        res_json = response.json()

        records_len = len(list(extract_jsonpath(self.records_jsonpath, res_json)))
        if records_len:
            return previous_token + records_len

    def get_starting_time(self, context):
        """Get the starting time for data extraction."""
        start_date = self.config.get("start_date")
        if start_date:
            start_date = parse(self.config.get("start_date"))
        rep_key = self.get_starting_timestamp(context)
        return rep_key or start_date

    def get_url_params(
        self, context: Optional[dict], next_page_token: Optional[Any]
    ) -> Dict[str, Any]:
        """Return a dictionary of values to be used in URL parameterization."""
        params: dict = {}
        if self.additional_params:
            params.update(self.additional_params)
        if next_page_token:
            params["offset"] = next_page_token
        # filter by date
        start_date = self.get_starting_time(context)
        if start_date and self.replication_key:
            start_date = start_date + datetime.timedelta(seconds=1)
            start_date = start_date.strftime("%Y-%m-%dT%H:%M:%SZ")
            params[f"{self.replication_key}[gt]"] = start_date
        return params
    
    def response_error_message(self, response: requests.Response) -> str:
        """Generate an error message from a failed HTTP response."""
        if 400 <= response.status_code < 500:
            error_type = "Client"
        else:
            error_type = "Server"

        return (
            f"{response.status_code} {error_type} Error: "
            f"{response.reason} for url: {response.url} "
            f"Response: {response.text}"
        )
        