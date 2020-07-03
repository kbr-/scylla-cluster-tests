import time
from sdcm.tester import ClusterTester

class GeminiTest(ClusterTester):
    def test_gemini(self):
        """
        Run gemini tool
        """
        cmd = self.params.get('gemini_cmd')

        self.log.info('Start gemini benchmark')
        gemini_thread = self.run_gemini(cmd=cmd)

        self.log.info('waiting for gemini to create schema...')
        time.sleep(10)

        self.log.info('verifying gemini results')
        gemini_results = self.verify_gemini_results(queue=gemini_thread)

        if gemini_results['status'] == 'FAILED':
            self.fail(self.gemini_results['results'])
