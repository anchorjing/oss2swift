# Copyright (c) 2014 OpenStack Foundation
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or
# implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import unittest

from oss2swift.response import Response as OssResponse
from swift.common.swob import Response


class TestRequest(unittest.TestCase):
    def test_from_swift_resp_slo(self):
        for expected, header_vals in \
                ((True, ('true', '1')), (False, ('false', 'ugahhh', None))):
            for val in header_vals:
                resp = Response(headers={'X-Static-Large-Object': val})
                ossresp = OssResponse.from_swift_resp(resp)
                self.assertEqual(expected, ossresp.is_slo)


if __name__ == '__main__':
    unittest.main()
