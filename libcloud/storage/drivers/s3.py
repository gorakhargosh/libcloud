# Licensed to the Apache Software Foundation (ASF) under one or more
# contributor license agreements.  See the NOTICE file distributed with
# this work for additional information regarding copyright ownership.
# The ASF licenses this file to You under the Apache License, Version 2.0
# (the "License"); you may not use this file except in compliance with
# the License.  You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import time
import httplib
import copy
import base64
import hmac

from hashlib import sha1
from xml.etree.ElementTree import Element, SubElement, tostring

from libcloud.utils import fixxpath, findtext, in_development_warning
from libcloud.common.types import InvalidCredsError, LibcloudError
from libcloud.common.base import ConnectionUserAndKey
from libcloud.common.aws import AWSBaseResponse

from libcloud.storage.base import Object, Container, StorageDriver
from libcloud.storage.types import ContainerIsNotEmptyError
from libcloud.storage.types import ContainerDoesNotExistError

in_development_warning('libcloud.storage.drivers.s3')

# How long before the token expires
EXPIRATION_SECONDS = 15 * 60

S3_US_STANDARD_HOST = 's3.amazonaws.com'
S3_US_WEST_HOST = 's3-us-west-1.amazonaws.com'
S3_EU_WEST_HOST = 's3-eu-west-1.amazonaws.com'
S3_AP_SOUTHEAST_HOST = 's3-ap-southeast-1.amazonaws.com'
S3_AP_NORTHEAST_HOST = 's3-ap-northeast-1.amazonaws.com'

API_VERSION = '2006-03-01'
NAMESPACE = 'http://s3.amazonaws.com/doc/%s/' % (API_VERSION)


class S3Response(AWSBaseResponse):

    valid_response_codes = [ httplib.NOT_FOUND, httplib.CONFLICT ]

    def success(self):
        i = int(self.status)
        return i >= 200 and i <= 299 or i in self.valid_response_codes

    def parse_error(self):
        if self.status == 403:
            raise InvalidCredsError(self.body)
        elif self.status == 301:
            raise LibcloudError('This bucket is located in a different ' +
                                'region. Please use the correct driver.',
                                driver=S3StorageDriver)
        raise LibcloudError('Unknown error. Status code: %d' % (self.status),
                            driver=S3StorageDriver)

class S3Connection(ConnectionUserAndKey):
    """
    Repersents a single connection to the EC2 Endpoint
    """

    host = 's3.amazonaws.com'
    responseCls = S3Response

    def add_default_params(self, params):
        expires = str(int(time.time()) + EXPIRATION_SECONDS)
        headers = self.add_default_headers({})
        params['Signature'] = self._get_aws_auth_param(method=self.method,
                                                       headers=headers,
                                                       params=params,
                                                       expires=expires,
                                                       secret_key=self.key,
                                                       path=self.action)
        params['AWSAccessKeyId'] = self.user_id
        params['Expires'] = expires
        return params

    def _get_aws_auth_param(self, method, headers, params, expires,
                            secret_key, path='/'):
        """
        Signature = URL-Encode( Base64( HMAC-SHA1( YourSecretAccessKeyID, UTF-8-Encoding-Of( StringToSign ) ) ) );

        StringToSign = HTTP-VERB + "\n" +
            Content-MD5 + "\n" +
            Content-Type + "\n" +
            Expires + "\n" +
            CanonicalizedAmzHeaders +
            CanonicalizedResource;
        """
        special_header_keys = [ 'content-md5', 'content-type', 'date' ]
        special_header_values = { 'date': '' }

        headers_copy = copy.deepcopy(headers)
        for key, value in headers_copy.iteritems():
            if key.lower() in special_header_keys:
                special_header_values[key.lower()] = value.lower().strip()

        if not special_header_values.has_key('content-md5'):
            special_header_values['content-md5'] = ''

        if not special_header_values.has_key('content-type'):
            special_header_values['content-type'] = ''

        if expires:
            special_header_values['date'] = str(expires)

        keys_sorted = special_header_values.keys()
        keys_sorted.sort()

        buf = [ method ]
        for key in keys_sorted:
            value = special_header_values[key]
            buf.append(value)
        string_to_sign = '\n'.join(buf)

        string_to_sign = '%s\n%s' % (string_to_sign, path)
        b64_hmac = base64.b64encode(
            hmac.new(secret_key, string_to_sign, digestmod=sha1).digest()
        )
        return b64_hmac

class S3StorageDriver(StorageDriver):
    name = 'Amazon S3 (standard)'
    connectionCls = S3Connection
    hash_type = 'md5'
    ex_location_name = ''

    def list_containers(self):
        response = self.connection.request('/')
        if response.status == httplib.OK:
            containers = self._to_containers(obj=response.object,
                                             xpath='Buckets/Bucket')
            return containers

        raise LibcloudError('Unexpected status code: %s' % (response.status),
                            driver=self)

    def list_container_objects(self, container):
        response = self.connection.request('/%s' % (container.name))
        if response.status == httplib.OK:
            objects = self._to_objs(obj=response.object,
                                       xpath='Contents', container=container)
            return objects

        raise LibcloudError('Unexpected status code: %s' % (response.status),
                            driver=self)

    def create_container(self, container_name):
        root = Element('CreateBucketConfiguration')
        child = SubElement(root, 'LocationConstraint')
        child.text = self.ex_location_name

        response = self.connection.request('/%s' % (container_name),
                                           data=tostring(root),
                                           method='PUT')
        if response.status == httplib.OK:
            container = Container(name=container_name, extra=None, driver=self)
            return container
        elif response.status == httplib.CONFLICT:
            raise LibcloudError('Container with this name already exists.' +
                                'The name must be unique across all the ' +
                                'containers in the system')

        raise LibcloudError('Unexpected status code: %s' % (response.status),
                            driver=self)

    def delete_container(self, container):
        # All the objects in the container must be deleted first
        response = self.connection.request('/%s' % (container.name),
                                           method='DELETE')
        if response.status == httplib.NO_CONTENT:
            return True
        elif response.status == httplib.CONFLICT:
            raise ContainerIsNotEmptyError(value='Container must be empty' +
                                                  ' before it can be deleted.',
                                           container_name=container.name,
                                           driver=self)
        elif response.status == httplib.NOT_FOUND:
            raise ContainerDoesNotExistError(value=None,
                                             driver=self,
                                             container_name=container.name)

        return False

    def _to_containers(self, obj, xpath):
        return [ self._to_container(element) for element in \
                 obj.findall(fixxpath(xpath=xpath, namespace=NAMESPACE))]

    def _to_objs(self, obj, xpath, container):
        return [ self._to_obj(element, container) for element in \
                 obj.findall(fixxpath(xpath=xpath, namespace=NAMESPACE))]

    def _to_container(self, element):
        extra = {
            'creation_date': findtext(element=element, xpath='CreationDate',
                                      namespace=NAMESPACE)
        }

        container = Container(
                        name=findtext(element=element, xpath='Name',
                                      namespace=NAMESPACE),
                        extra=extra,
                        driver=self
                    )

        return container

    def _to_obj(self, element, container):
        owner_id = findtext(element=element, xpath='Owner/ID',
                            namespace=NAMESPACE)
        owner_display_name = findtext(element=element,
                                      xpath='Owner/DisplayName',
                                      namespace=NAMESPACE)
        meta_data = { 'owner': { 'id': owner_id,
                                 'display_name':owner_display_name }}

        obj = Object(name=findtext(element=element, xpath='Key',
                     namespace=NAMESPACE),
                     size=findtext(element=element, xpath='Size',
                     namespace=NAMESPACE),
                     hash=findtext(element=element, xpath='ETag',
                     namespace=NAMESPACE),
                     extra=None,
                     meta_data=meta_data,
                     container=container,
                     driver=self,
             )
        return obj


class S3USWestConnection(S3Connection):
    host = S3_US_WEST_HOST

class S3USWestStorageDriver(S3StorageDriver):
    name = 'Amazon S3 (us-west-1)'
    connectionCls = S3USWestConnection
    ex_location_name = 'us-west-1'

class S3EUWestConnection(S3Connection):
    host = S3_EU_WEST_HOST

class S3EUWestStorageDriver(S3StorageDriver):
    name = 'Amazon S3 (eu-west-1)'
    connectionCls = S3EUWestConnection
    ex_location_name = 'EU'

class S3APSEConnection(S3Connection):
    host = S3_AP_SOUTHEAST_HOST

class S3APSEStorageDriver(S3StorageDriver):
    name = 'Amazon S3 (ap-southeast-1)'
    connectionCls = S3APSEConnection
    ex_location_name = 'ap-southeast-1'

class S3APNEConnection(S3Connection):
    host = S3_AP_NORTHEAST_HOST

class S3APNEStorageDriver(S3StorageDriver):
    name = 'Amazon S3 (ap-northeast-1)'
    connectionCls = S3APNEConnection
    ex_location_name = 'ap-northeast-1'