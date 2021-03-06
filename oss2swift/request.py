import base64
from email.header import Header
from hashlib import sha256, md5
import os
import re
import string
import sys
from urllib import quote, unquote

from oss2swift.acl_handlers import get_acl_handler
from oss2swift.acl_utils import handle_acl_header
from oss2swift.acl_utils import swift_acl_translate
from oss2swift.cfg import CONF
from oss2swift.controllers import ServiceController, BucketController, \
    ObjectController, AclController, MultiObjectDeleteController, \
    LocationController, LoggingStatusController, PartController, \
    UploadController, UploadsController, VersioningController, \
    UnsupportedController, OssAclController, CorsController, LifecycleController,WebsiteController, RefererController
from oss2swift.exception import NotOssRequest, BadSwiftRequest, ACLError
from oss2swift.response import AccessDenied, InvalidArgument, InvalidDigest, \
    RequestTimeTooSkewed, Response, SignatureDoesNotMatch, \
    BucketAlreadyExists, BucketNotEmpty, EntityTooLarge, \
    InternalError, NoSuchBucket, NoSuchKey, PreconditionFailed, InvalidRange, \
    MissingContentLength, InvalidStorageClass, OssNotImplemented, InvalidURI, \
    MalformedXML, InvalidRequest, RequestTimeout, InvalidBucketName, \
    BadDigest, AuthorizationHeaderMalformed, AuthorizationQueryParametersError, MalformedACLError, \
    InvalidObjectName
from oss2swift.subresource import decode_acl, encode_acl
from oss2swift.utils import sysmeta_header, validate_bucket_name
from oss2swift.utils import utf8encode, LOGGER, check_path_header, OssTimestamp, \
    mktime
import six
from swift.common import swob
from swift.common.constraints import check_utf8
from swift.common.http import HTTP_OK, HTTP_CREATED, HTTP_ACCEPTED, \
    HTTP_NO_CONTENT, HTTP_UNAUTHORIZED, HTTP_FORBIDDEN, HTTP_NOT_FOUND, \
    HTTP_CONFLICT, HTTP_UNPROCESSABLE_ENTITY, HTTP_REQUEST_ENTITY_TOO_LARGE, \
    HTTP_PARTIAL_CONTENT, HTTP_NOT_MODIFIED, HTTP_PRECONDITION_FAILED, \
    HTTP_REQUESTED_RANGE_NOT_SATISFIABLE, HTTP_LENGTH_REQUIRED,HTTP_MOVED_PERMANENTLY, \
    HTTP_BAD_REQUEST, HTTP_REQUEST_TIMEOUT, is_success
from swift.common.utils import split_path,json
from swift.proxy.controllers.base import get_container_info, \
    headers_to_container_info


# List of sub-resources that must be maintained as part of the HMAC
# signature string.
ALLOWED_SUB_RESOURCES = sorted([
    'acl', 'delete', 'lifecycle', 'location', 'logging', 'notification',
    'partNumber', 'policy', 'requestPayment', 'torrent', 'uploads', 'uploadId',
    'versionId', 'versioning', 'versions', 'website', 'objectMeta','referer',
    'response-cache-control', 'response-content-disposition',
    'response-content-encoding', 'response-content-language',
    'response-content-type', 'response-expires', 'cors', 'tagging', 'restore'
])
CAN_NOT_CAPTURE='cnc'
MAX_ACL_BODY_SIZE = 200 * 1024
MAX_32BIT_INT = 2147483647
X_OSS_DATE_FORMAT = '%Y-%m-%dT%H:%M:%S'
X_OSS_DATE_FORMAT2 = '%Y%m%dT%H%M%SZ'


def _header_acl_property(resource):
    """
    Set and retrieve the acl in self.headers
    """
    def getter(self):
        return getattr(self, '_%s' % resource)

    def setter(self, value):
        self.headers.update(encode_acl(resource, value))
        setattr(self, '_%s' % resource, value)

    def deleter(self):
        self.headers[sysmeta_header(resource, 'acl')] = ''

    return property(getter, setter, deleter,
                    doc='Get and set the %s acl property' % resource)


def get_request_class(env):
    if CONF.oss_acl:
        return OssAclRequest
    else:
        return Request


class Request(swob.Request):
    """
    Oss request object.
    """

    bucket_acl = _header_acl_property('container')
    object_acl = _header_acl_property('object')

    def __init__(self, env, app=None, slo_enabled=True):
        # NOTE: app is not used by this class, need for compatibility of Ossacl
        swob.Request.__init__(self, env)
        self.req = swob.Request(env)
        self._timestamp = None
        self.access_key, signature = self._parse_auth_info()
        self.bucket_in_host = self._parse_host()
        self.container_name, self.object_name = self._parse_uri()
        self._validate_headers()
        self.token = base64.urlsafe_b64encode(self._string_to_sign())
        self.account = None
        self.user_id = None
        self.slo_enabled = slo_enabled
        self.headers['Authorization'] = 'OSS %s:%s' % (
            self.access_key, signature)
        self.environ['swift.leave_relative_location'] = True
    @property
    def timestamp(self):
        if not self._timestamp:
            try:
                if self._is_query_auth and 'Timestamp' in self.params:
                    timestamp = mktime(
                        self.params['Timestamp'], X_OSS_DATE_FORMAT)
                else:
                    timestamp = mktime(
                        self.headers.get('X-Oss-Date',
                                         self.headers.get('Date')))
            except ValueError:
                raise AccessDenied('OSS authentication requires a valid Date '
                                   'or x-oss-date header')

            try:
                self._timestamp = OssTimestamp(timestamp)
            except ValueError:
                raise AccessDenied()

        return self._timestamp

    @property
    def _is_header_auth(self):
        return 'Authorization' in self.headers

    @property
    def _is_query_auth(self):
        return 'OSSAccessKeyId' in self.params

    def _parse_host(self):
        storage_domain = CONF.storage_domain
        if not storage_domain:
            return None

        if not storage_domain.startswith('.'):
            storage_domain = '.' + storage_domain

        if 'HTTP_HOST' in self.environ:
            given_domain = self.environ['HTTP_HOST']
        elif 'SERVER_NAME' in self.environ:
            given_domain = self.environ['SERVER_NAME']
        else:
            return None

        port = ''
        if ':' in given_domain:
            given_domain, port = given_domain.rsplit(':', 1)
        if given_domain.endswith(storage_domain):
            return given_domain[:-len(storage_domain)]

        return None
    
    def _parse_uri(self):
        if not check_utf8(self.environ['PATH_INFO']):
            raise InvalidURI(self.path)

        if self.bucket_in_host:
            obj = self.environ['PATH_INFO'][1:] or None
            return self.bucket_in_host, obj

        bucket, obj = self.split_path(0, 2, True)

        if bucket and not validate_bucket_name(bucket):
            # Ignore GET service case
            raise InvalidBucketName(bucket)
        return (bucket, obj)
    
    def _parse_query_authentication(self):
        try:
            access = self.params['OSSAccessKeyId']
            expires = self.params['Expires']
            sig = self.params['Signature']
        except KeyError:
            raise AccessDenied()

        if not all([access, sig, expires]):
            raise AccessDenied()

        return access, sig

    def _parse_header_authentication(self):
        auth_str = self.headers['Authorization']
        if not auth_str.startswith('OSS ') or ':' not in auth_str:
            raise AccessDenied()
        # This means signature format V2
        access, sig = auth_str.split(' ', 1)[1].rsplit(':', 1)
        return access, sig

    def _parse_auth_info(self):
        """Extract the access key identifier and signature.

        :returns: a tuple of access_key and signature
        :raises: NotOssRequest
        """
        if self._is_query_auth:
            return self._parse_query_authentication()
        elif self._is_header_auth:
            return self._parse_header_authentication()
        else:
            # if this request is neither query auth nor header auth
            # oss2swift regard this as not oss request
            raise NotOssRequest()

    def _validate_expire_param(self):
        """
        Validate Expires in query parameters
        :raises: AccessDenied
        """
        # Expires header is a float since epoch
        try:
            ex = OssTimestamp(float(self.params['Expires']))
        except ValueError:
            raise AccessDenied()

        if OssTimestamp.now() > ex:
            raise AccessDenied('Request has expired')

        if ex >= 2 ** 31:
            raise AccessDenied(
                'Invalid date (should be seconds since epoch): %s' %
                self.params['Expires'])

    def _validate_dates(self):
        """
        Validate Date/X-Oss-Date headers for signature v2
        :raises: AccessDenied
        :raises: RequestTimeTooSkewed
        """
        if self._is_query_auth:
            self._validate_expire_param()
            # TODO: make sure the case if timestamp param in query
            return

        date_header = self.headers.get('Date')
        oss_date_header = self.headers.get('X-Oss-Date')
        if not date_header and not oss_date_header:
            raise AccessDenied('OSS authentication requires a valid Date '
                               'or x-oss-date header')

        # Anyways, request timestamp should be validated
        epoch = OssTimestamp(0)
        if self.timestamp < epoch:
            raise AccessDenied()

        # If the standard date is too far ahead or behind, it is an
        # error
        delta = 60 * 5
        if abs(int(self.timestamp) - int(OssTimestamp.now())) > delta:
            raise RequestTimeTooSkewed()

    def _validate_headers(self):
        if 'CONTENT_LENGTH' in self.environ:
            try:
                if self.content_length < 0:
                    raise InvalidArgument('Content-Length',
                                          self.content_length)
            except (ValueError, TypeError):
                raise InvalidArgument('Content-Length',
                                      self.environ['CONTENT_LENGTH'])

        self._validate_dates()

        if 'Content-MD5' in self.headers:
            value = self.headers['Content-MD5']
            if not re.match('^[A-Za-z0-9+/]+={0,2}$', value):
                # Non-base64-alphabet characters in value.
                raise InvalidDigest(content_md5=value)
            try:
                self.headers['ETag'] = value.decode('base64').encode('hex')
            except Exception:
                raise InvalidDigest(content_md5=value)

            if len(self.headers['ETag']) != 32:
                raise InvalidDigest(content_md5=value)

        if self.method == 'PUT' and any(h in self.headers for h in (
                'If-Match', 'If-None-Match',
                'If-Modified-Since', 'If-Unmodified-Since')):
            raise OssNotImplemented(
                'Conditional object PUTs are not supported.')

        if 'X-Oss-Copy-Source' in self.headers:
            try:
                check_path_header(self, 'X-Oss-Copy-Source', 2, '')
            except swob.HTTPException:
                msg = 'Copy Source must mention the source bucket and key: ' \
                      'sourcebucket/sourcekey'
                raise InvalidArgument('x-oss-copy-source',
                                      self.headers['X-Oss-Copy-Source'],
                                      msg)

        if 'x-oss-metadata-directive' in self.headers:
            value = self.headers['x-oss-metadata-directive']
            if value not in ('COPY', 'REPLACE'):
                err_msg = 'Unknown metadata directive.'
                raise InvalidArgument('x-oss-metadata-directive', value,
                                      err_msg)

        if 'x-oss-storage-class' in self.headers:
            # Only STANDARD is supported now.
            if self.headers['x-oss-storage-class'] != 'STANDARD':
                raise InvalidStorageClass()

        if 'x-oss-mfa' in self.headers:
            raise OssNotImplemented('MFA Delete is not supported.')

        if 'x-oss-server-side-encryption' in self.headers:
            raise OssNotImplemented('Server-side encryption is not supported.')


    @property
    def body(self):
        """
        swob.Request.body is not secure against malicious input.  It consumes
        too much memory without any check when the request body is excessively
        large.  Use xml() instead.
        """
        return self.req.body
        # raise AttributeError("No attribute 'body'")

    def xml(self, max_length, check_md5=False):
        """
        Similar to swob.Request.body, but it checks the content length before
        creating a body string.
        """
        te = self.headers.get('transfer-encoding', '')
        te = [x.strip() for x in te.split(',') if x.strip()]
        if te and (len(te) > 1 or te[-1] != 'chunked'):
            raise OssNotImplemented('A header you provided implies '
                                   'functionality that is not implemented',
                                   header='Transfer-Encoding')

        if self.message_length() > max_length:
            raise MalformedXML()

        # Limit the read similar to how SLO handles manifests
        body = self.body_file.read(max_length)

        if check_md5:
            self.check_md5(body)

        return body

    def check_md5(self, body):
        if 'HTTP_CONTENT_MD5' not in self.environ:
            raise InvalidRequest('Missing required header for this request: '
                                 'Content-MD5')

        digest = md5(body).digest().encode('base64').strip()
        if self.environ['HTTP_CONTENT_MD5'] != digest:
            raise BadDigest(content_md5=self.environ['HTTP_CONTENT_MD5'])

    def _copy_source_headers(self):
        env = {}
        for key, value in self.environ.items():
            if key.startswith('HTTP_X_OSS_COPY_SOURCE_'):
                env[key.replace('X_OSS_COPY_SOURCE_', '')] = value

        return swob.HeaderEnvironProxy(env)

    def get_bucket_info(self, app):
        bucket_resp = self.get_response(app, 'HEAD', self.container_name)
        return bucket_resp.headers

    def check_copy_source(self, app):
        """
        check_copy_source checks the copy source existence and if copying an
        object to itself, for illegal request parameters

        :returns: the source HEAD response
        """
        if 'x-oss-copy-source' not in self.headers:
            self.req.headers['x-object-meta-object-type'] = 'Normal'
            return None

        src_path = unquote(self.headers['x-oss-copy-source'])
        src_path = src_path if src_path.startswith('/') else \
            ('/' + src_path)
        src_bucket, src_obj = split_path(src_path, 0, 2, True)
        headers = swob.HeaderKeyDict()
        headers.update(self._copy_source_headers())

        src_resp = self.get_response(app, 'HEAD', src_bucket, src_obj,
                                     headers=headers)
        if src_resp.status_int == 304:  # pylint: disable-msg=E1101
            raise PreconditionFailed()

        self.headers['x-oss-copy-source'] = \
            '/' + self.headers['x-oss-copy-source'].lstrip('/')
        source_container, source_obj = \
            split_path(self.headers['x-oss-copy-source'], 1, 2, True)

        return src_resp

    def _canonical_uri(self):
        """
        Require bucket name in canonical_uri for v2 in virtual hosted-style.
        """
        raw_path_info = self.environ.get('RAW_PATH_INFO', self.path)
        if self.bucket_in_host:
            raw_path_info = '/' + self.bucket_in_host + raw_path_info
        return unquote(raw_path_info)

    def _string_to_sign(self):
        """
        Create 'StringToSign' value in Amazon terminology for v2.
        """
        oss_headers = {}

        buf = "%s\n%s\n%s\n" % (self.method,
                                self.headers.get('Content-MD5', ''),
                                self.headers.get('Content-Type') or '')

        for oss_header in sorted((key.lower() for key in self.headers
                                  if key.lower().startswith('x-oss-'))):
            oss_headers[oss_header] = self.headers[oss_header]

        if self._is_header_auth:
            if 'x-oss-date' in oss_headers:
                buf += "\n"
            elif 'Date' in self.headers:
                buf += "%s\n" % self.headers['Date']
        elif self._is_query_auth:
            buf += "%s\n" % self.params['Expires']
        else:
            # Should have already raised NotOssRequest in _parse_auth_info,
            # but as a sanity check...
            raise AccessDenied()

        for k in sorted(key.lower() for key in oss_headers):
            buf += "%s:%s\n" % (k, oss_headers[k])

        path = self._canonical_uri()
        if self.query_string:
            path += '?' + self.query_string
        if '?' in path:
            path, args = path.split('?', 1)
            params = []
            for key, value in sorted(self.params.items()):
                if key in ALLOWED_SUB_RESOURCES:
                    params.append('%s=%s' % (key, value) if value else key)
            if params:
                return '%s%s?%s' % (buf, path, '&'.join(params))

        return buf + path

    @property
    def controller_name(self):
        return self.controller.__name__[:-len('Controller')]

    @property
    def controller(self):
        if self.is_service_request:
            return ServiceController

        if not self.slo_enabled:
            multi_part = ['partNumber', 'uploadId', 'uploads']
            if len([p for p in multi_part if p in self.params]):
                LOGGER.warning('multipart: No SLO middleware in pipeline')
                raise OssNotImplemented("Multi-part feature isn't support")
        # if 'objectMeta' in self.params:
        #     return ObjectController
        if 'acl' in self.params:
            return AclController
        if 'cors' in self.params:
            return CorsController
        if 'delete' in self.params:
            return MultiObjectDeleteController
        if 'location' in self.params:
            return LocationController
        if 'logging' in self.params:
            return LoggingStatusController
        if 'partNumber' in self.params:
            return PartController
        if 'uploadId' in self.params:
            return UploadController
        if 'uploads' in self.params:
            return UploadsController
        if 'versioning' in self.params:
            return VersioningController
        if 'lifecycle' in self.params:
            return LifecycleController
        if 'website' in self.params:
            return WebsiteController
        if 'referer' in self.params:
            return RefererController
        
        unsupported = ('notification', 'policy', 'requestPayment', 'torrent',
                        'tagging', 'restore')
        if set(unsupported) & set(self.params):
            return UnsupportedController

        if self.is_object_request:
            return ObjectController
        return BucketController

    @property
    def is_service_request(self):
        return not self.container_name

    @property
    def is_bucket_request(self):
        return self.container_name and not self.object_name

    @property
    def is_object_request(self):
        return self.container_name and self.object_name

    @property
    def is_authenticated(self):
        return self.account is not None

    def to_swift_req(self, method, container, obj, query=None,
                     body=None, headers=None):
        """
        Create a Swift request based on this request's environment.
        """
        if self.account is None:
            account = self.access_key
        else:
            account = self.account
        env = self.environ.copy()

        for key in self.environ:
            if key.startswith('HTTP_X_OSS_META_'):
                if not(set(env[key]).issubset(string.printable)):
                    env[key] = Header(env[key], 'UTF-8').encode()
                    if env[key].startswith('=?utf-8?q?'):
                        env[key] = '=?UTF-8?Q?' + env[key][10:]
                    elif env[key].startswith('=?utf-8?b?'):
                        env[key] = '=?UTF-8?B?' + env[key][10:]
                env['HTTP_X_OBJECT_META_' + key[16:]] = env[key]
                del env[key]
        if 'HTTP_X_OSS_COPY_SOURCE' in env:
            env['HTTP_X_COPY_FROM'] = env['HTTP_X_OSS_COPY_SOURCE']
            del env['HTTP_X_OSS_COPY_SOURCE']
            env['CONTENT_LENGTH'] = '0'

        if CONF.force_swift_request_proxy_log:
            env['swift.proxy_access_log_made'] = False
        env['swift.source'] = 'Oss'
        if method is not None:
            env['REQUEST_METHOD'] = method

        env['HTTP_X_AUTH_TOKEN'] = self.token

        if obj:
            path = '/v1/%s/%s/%s' % (account, container, obj)
        elif container:
            path = '/v1/%s/%s' % (account, container)
        else:
            path = '/v1/%s' % (account)
        env['PATH_INFO'] = path

        query_string = ''
        if query is not None:
            params = []
            for key, value in sorted(query.items()):
                if value is not None:
                    params.append('%s=%s' % (key, quote(str(value))))
                else:
                    params.append(key)
            query_string = '&'.join(params)
        env['QUERY_STRING'] = query_string
        return swob.Request.blank(quote(path), environ=env, body=body,
                                  headers=headers)

    def _swift_success_codes(self, method, container, obj):
        """
        Returns a list of expected success codes from Swift.
        """
        if not container:
            # Swift account access.
            code_map = {
                'GET': [
                    HTTP_OK,
                ],
            }
        elif not obj:
            # Swift container access.
            code_map = {
                'HEAD': [
                    HTTP_NO_CONTENT,
                ],
                'GET': [
                    HTTP_OK,
                    HTTP_NO_CONTENT,
                ],
                'PUT': [
                    HTTP_CREATED,
                    HTTP_ACCEPTED,
                ],
                'POST': [
                    HTTP_NO_CONTENT,
                ],
                'DELETE': [
                    HTTP_NO_CONTENT,
                ],
            }
        else:
            # Swift object access.
            code_map = {
                'HEAD': [
                    HTTP_OK,
                    HTTP_PARTIAL_CONTENT,
                    HTTP_NOT_MODIFIED,
                ],
                'GET': [
                    HTTP_OK,
                    HTTP_PARTIAL_CONTENT,
                    HTTP_NOT_MODIFIED,
                ],
                'PUT': [
                    HTTP_CREATED,
                ],
                'POST': [
                    HTTP_ACCEPTED,
                ],
                'DELETE': [
                    HTTP_OK,
                    HTTP_NO_CONTENT,
                ],
            }

        return code_map[method]

    def _swift_error_codes(self, method, container, obj):
        """
        Returns a dict from expected Swift error codes to the corresponding Oss
        error responses.
        """
        if not container:
            # Swift account access.
            code_map = {
                'GET': {
                },
            }
        elif not obj:
            # Swift container access.
            code_map = {
                'HEAD': {
                    HTTP_NOT_FOUND: (NoSuchBucket, container),
                },
                'GET': {
                    HTTP_NOT_FOUND: (NoSuchBucket, container),
                },
                'PUT': {
                    HTTP_ACCEPTED: (BucketAlreadyExists, container),
                },
                'POST': {
                    HTTP_NOT_FOUND: (NoSuchBucket, container),
                },
                'DELETE': {
                    HTTP_NOT_FOUND: (NoSuchBucket, container),
                    HTTP_CONFLICT: BucketNotEmpty,
                },
            }
        else:
            # Swift object access.
            code_map = {
                'HEAD': {
                    HTTP_NOT_FOUND: (NoSuchKey, obj),
                    HTTP_PRECONDITION_FAILED: PreconditionFailed,
                },
                'GET': {
                    HTTP_NOT_FOUND: (NoSuchKey, obj),
                    HTTP_PRECONDITION_FAILED: PreconditionFailed,
                    HTTP_REQUESTED_RANGE_NOT_SATISFIABLE: InvalidRange,
                },
                'PUT': {
                    HTTP_UNPROCESSABLE_ENTITY: InvalidDigest,
                    HTTP_REQUEST_ENTITY_TOO_LARGE: EntityTooLarge,
                    HTTP_LENGTH_REQUIRED: MissingContentLength,
                    HTTP_REQUEST_TIMEOUT: RequestTimeout,
                },
                'POST': {
                    HTTP_NOT_FOUND: (NoSuchKey, obj),
                    HTTP_PRECONDITION_FAILED: PreconditionFailed,
                },
                'DELETE': {
                    HTTP_NOT_FOUND: (NoSuchKey, obj),
                },
            }

        return code_map[method]

    def _get_response(self, app, method, container, obj,
                      headers=None, body=None, query=None):
        """
        Calls the application with this request's environment.  Returns a
        Response object that wraps up the application's result.
        """

        method = method or self.environ['REQUEST_METHOD']
        if container is None:
            container = self.container_name
        if obj is None:
            obj = self.object_name
        if str(obj).startswith('/'):
            raise InvalidObjectName
        
        sw_req = self.to_swift_req(method, container,obj, headers=headers,
                                    body=body, query=query)
        if container and obj:
	       # before obj request container
           req = self.to_swift_req(method, container,obj='', headers=headers,
                                   body=body, query=query)
           resp = req.get_response(app)
    	   if resp.status_int==HTTP_NOT_FOUND:
                raise NoSuchBucket(container)
	   else:
		sw_resp	=sw_req.get_response(app)
	else:
 	   sw_resp=sw_req.get_response(app)
           # reuse account and tokens
        _, self.account, _ = split_path(sw_resp.environ['PATH_INFO'],
                                        2, 3, True)
        self.account = utf8encode(self.account)

        resp = Response.from_swift_resp(sw_resp)
        if 'X-Container-Read' in sw_resp.headers:
            resp.headers['X-Container-Read']=sw_resp.headers['X-Container-Read']
        if 'X-Container-Write' in sw_resp.headers:
            resp.headers['X-Container-Write']=sw_resp.headers['X-Container-Write']
        status = resp.status_int  # pylint: disable-msg=E1101

        if not self.user_id:
               if 'HTTP_X_USER_NAME' in sw_resp.environ:
                   # keystone
                   self.user_id = \
                       utf8encode("%s:%s" %
                                  (sw_resp.environ['HTTP_X_TENANT_NAME'],
                                   sw_resp.environ['HTTP_X_USER_NAME']))
               else:
                   # tempauth
                   self.user_id = self.access_key

        success_codes = self._swift_success_codes(method, container, obj)
        error_codes = self._swift_error_codes(method, container, obj)

        if status in success_codes:
               return resp

        for key in resp.environ.keys():
            if 'swift.container' in key :
                if 'meta'.decode("utf8") in resp.environ[key]:
                    if 'web-index'.decode("utf8") in resp.environ[key]['meta'.decode("utf8")]:
                        resp.headers['x-oss-web-index']=resp.environ[key]['meta'.decode("utf8")]['web-index'.decode("utf8")]
                    if 'web-error'.decode("utf8") in resp.environ[key]['meta'.decode("utf8")]:
                        resp.headers['x-oss-web-error']=resp.environ[key]['meta'.decode("utf8")]['web-error'.decode("utf8")]
                        if status==HTTP_NOT_FOUND:
                            if obj.endswith('/'):
                                resp.headers['x-oss-index']=resp.environ[key]['meta'.decode("utf8")]['web-index'.decode("utf8")]
                            return resp
        err_msg = resp.body
        if status in error_codes:
            err_resp = \
                error_codes[sw_resp.status_int]  # pylint: disable-msg=E1101
            if isinstance(err_resp, tuple):
                raise err_resp[0](*err_resp[1:])
            else:
                raise err_resp()
        if status == HTTP_BAD_REQUEST:
            raise BadSwiftRequest(err_msg)
        if status == HTTP_UNAUTHORIZED:
            raise SignatureDoesNotMatch()
        if status == HTTP_FORBIDDEN:
            raise AccessDenied()
        if status == HTTP_MOVED_PERMANENTLY:
            resp.headers['x-oss-website-redirect']='true'
            return resp

        raise InternalError('unexpected status code %d' % status)

    def get_response(self, app, method=None, container=None, obj=None,
                     headers=None, body=None, query=None):
        """
        get_response is an entry point to be extended for child classes.
        If additional tasks needed at that time of getting swift response,
        we can override this method. oss2swift.request.Request need to just call
        _get_response to get pure swift response.
        """
        if 'HTTP_X_OSS_ACL' in self.environ:
            handle_acl_header(self)

        return self._get_response(app, method, container, obj,
                                  headers, body, query)

    def get_validated_param(self, param, default, limit=MAX_32BIT_INT):
        value = default
        if param in self.params:
            try:
                if value < int(self.params[param]):
                    value = int(self.params[param])
                if value < 0:
                    err_msg = 'Argument %s must be an integer between 0 and' \
                              ' %d' % (param, MAX_32BIT_INT)
                    raise InvalidArgument(param, self.params[param], err_msg)

                if value > MAX_32BIT_INT:
                    # check the value because int() could build either a long
                    # instance or a 64bit integer.
                    raise ValueError()

                if limit < value:
                    value = limit

            except ValueError:
                err_msg = 'Provided %s not an integer or within ' \
                          'integer range' % param
                raise InvalidArgument(param, self.params[param], err_msg)

        return value

    def get_container_info(self, app):

        if self.is_authenticated:
            # if we have already authenticated, yes we can use the account
            # name like as AUTH_xxx for performance efficiency
            sw_req = self.to_swift_req(app, self.container_name, None)
            info = get_container_info(sw_req.environ, app)
            if is_success(info['status']):
                return info
            elif info['status'] == 404:
                raise NoSuchBucket(self.container_name)
            else:
                raise InternalError(
                    'unexpected status code %d' % info['status'])
        else:
            # otherwise we do naive HEAD request with the authentication
            resp = self.get_response(app, 'HEAD', self.container_name, '')
            return headers_to_container_info(
                resp.sw_headers, resp.status_int)  # pylint: disable-msg=E1101

    def gen_multipart_manifest_delete_query(self, app):
        if not CONF.allow_multipart_uploads:
            return None
        query = {'multipart-manifest': 'delete'}
        resp = self.get_response(app, 'HEAD')
        return query if resp.is_slo else None


class OssAclRequest(Request):
    """
    OssAcl request object.
    """
    def __init__(self, env, app, slo_enabled=True):
        super(OssAclRequest, self).__init__(env, slo_enabled)
	if app is not None:

           self.authenticate(app)
    @property
    def controller(self):
        if 'acl' in self.params and not self.is_service_request:
            return OssAclController
        return super(OssAclRequest, self).controller

    def authenticate(self, app):
        """
        authenticate method will run pre-authenticate request and retrieve
        account information.
        Note that it currently supports only keystone and tempauth.
        (no support for the third party authentication middleware)
        """
        sw_req = self.to_swift_req('TEST', None, None, body='')
        # don't show log message of this request
        sw_req.environ['swift.proxy_access_log_made'] = True

        sw_resp = sw_req.get_response(app)

        if not sw_req.remote_user:
            raise SignatureDoesNotMatch()

        _, self.account, _ = split_path(sw_resp.environ['PATH_INFO'],
                                        2, 3, True)
        self.account = utf8encode(self.account)

        if 'HTTP_X_USER_NAME' in sw_resp.environ:
            # keystone
            self.user_id = "%s:%s" % (sw_resp.environ['HTTP_X_TENANT_NAME'],
                                      sw_resp.environ['HTTP_X_USER_NAME'])
            self.user_id = utf8encode(self.user_id)
            self.token = sw_resp.environ['HTTP_X_AUTH_TOKEN']
            # Need to skip Oss authorization since authtoken middleware
            # overwrites account in PATH_INFO
            del self.headers['Authorization']
        else:
            # tempauth
            self.user_id = self.access_key

    def to_swift_req(self, method, container, obj, query=None,
                     body=None, headers=None):
        sw_req = super(OssAclRequest, self).to_swift_req(
            method, container, obj, query, body, headers)
        if self.account:
            sw_req.environ['swift_owner'] = True  # needed to set ACL
            sw_req.environ['swift.authorize_override'] = True
            sw_req.environ['swift.authorize'] = lambda req: None
        if 'HTTP_X_CONTAINER_SYSMETA_OSS2SWIFT_ACL' in sw_req.environ:

            oss_acl = sw_req.environ['HTTP_X_CONTAINER_SYSMETA_OSS2SWIFT_ACL']
            if sw_req.query_string:
                sw_req.query_string = ''
            if oss_acl =='[]':
                oss_acl='private'
            try:
                translated_acl = swift_acl_translate(oss_acl)
            except ACLError:
                raise InvalidArgument('x-oss-acl', oss_acl)

            for header, acl in translated_acl:
                sw_req.headers[header] = acl
        return sw_req

    def get_acl_response(self, app, method=None, container=None, obj=None,
                         headers=None, body=None, query=None):
        """
        Wrapper method of _get_response to add Oss acl information
        from response sysmeta headers.
        """

        resp = self._get_response(
            app, method, container, obj, headers, body, query)
        if 'X-Container-Read' and 'X-Container-Write' not in resp.headers:
            resp.sysmeta_copy_headers='private'
        if'X-Container-Read' in resp.headers:
            if resp.headers['X-Container-Read'] == '.rlistings' or '.r:*':
                resp.sysmeta_copy_headers='public-read'
                if 'X-Container-Write' in resp.headers and resp.headers['X-Container-Write']=='.r:*':
                    resp.sysmeta_copy_headers='public-read-write'
            elif resp.headers['X-Container-Read'] == ' ' and resp.headers['X-Container-Read']==' ':
                resp.sysmeta_copy_headers='private'
            else:
                resp.sysmeta_copy_headers=CAN_NOT_CAPTURE
        resp.owner=self.user_id
        resp.bucket_acl = decode_acl('container', resp.sysmeta_headers,resp.owner)
        resp.object_acl = decode_acl('object', resp.sysmeta_headers,resp.owner)

        return resp

    def get_response(self, app, method=None, container=None, obj=None,
                     headers=None, body=None, query=None):
        """
        Wrap up get_response call to hook with acl handling method.
        """
        acl_handler = get_acl_handler(self.controller_name)(
            self, container, obj, headers)
        resp = acl_handler.handle_acl(app, method)

        # possible to skip recalling get_response_acl if resp is not
        # None (e.g. HEAD)
        if resp:
            return resp
        return self.get_acl_response(app, method, container, obj,
                                     headers, body, query)


