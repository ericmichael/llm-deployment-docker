from django.contrib.auth import get_user_model
from django.urls import reverse
from rest_framework.test import APITestCase
from rest_framework.authtoken.models import Token
from chat.tests.unit.vcr_config import vcr

class OpenAIAPITest(APITestCase):
    def setUp(self):
        # Create a user and a corresponding token
        self.user = get_user_model().objects.create_user(email='testuser@test.com', password='12345')
        self.token = Token.objects.create(user=self.user)
        self.api_url = reverse('openai_api_chat_completions_passthrough')

    @vcr.use_cassette('chat/tests/fixtures/vcr_cassettes/openai_api_chat_completions.yaml', filter_headers=[('authorization', None)])
    def test_openai_api_chat_completions_passthrough(self):
        # Define the request data
        request_data = {
            "messages": [
                {"role": "system", "content": "You are a helpful assistant."},
                {"role": "user", "content": "Who won the world series in 2020?"}
            ],
            "model": "gpt-5"
        }

        # Make a POST request to the API
        response = self.client.post(
            self.api_url,
            request_data,
            format='json',
            HTTP_AUTHORIZATION='Bearer ' + self.token.key
        )

        # Assert that the response status code is 200
        self.assertEqual(response.status_code, 200)

        # Assert that the response data is as expected
        self.assertIn('choices', response.data)
