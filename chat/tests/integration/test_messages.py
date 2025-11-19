from django.test import TestCase
from django.urls import reverse
from django.contrib.auth import get_user_model
from chat.models import Thread, Message
from chat.tests.unit.vcr_config import vcr

class MessageIntegrationTestCase(TestCase):
    def setUp(self):
        # Create a user for the tests
        self.user = get_user_model().objects.create_user(email='testuser@test.com', password='12345')
        # Log the user in
        self.client.login(username='testuser@test.com', password='12345')
        # Create a thread
        self.thread = Thread.objects.create(name='Test Thread', user=self.user)

    @vcr.use_cassette('chat/tests/fixtures/vcr_cassettes/message_creation.yaml', filter_headers=[('authorization', None)])
    def test_message_creation(self):
        # Test that a user can create a message within a thread
        response = self.client.post(reverse('new_message', kwargs={'pk': self.thread.pk}), {'content': 'Hello, World!'})
        self.assertEqual(response.status_code, 302)  # Expect a redirect after creation

        # Check that the message was created and is associated with the correct user and thread
        message_exists = Message.objects.filter(
            content='Hello, World!',
            user=self.user,
            thread=self.thread
        ).exists()
        self.assertTrue(message_exists)  # Message should exist in the database and be associated with the correct user and thread

    def test_thread_view_with_messages(self):
        # Create a message within the thread
        Message.objects.create(thread=self.thread, user=self.user, content='Hello, World!')

        # Test that the thread detail page shows the message
        response = self.client.get(reverse('thread_detail', kwargs={'pk': self.thread.pk}))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Hello, World!')  # The message content should be on the page

    def tearDown(self):
        # Clean up after each test method
        self.client.logout()

class MessageNonAuthenticatedTestCase(TestCase):
    def setUp(self):
        # Create a user and a thread but do not log in
        self.user = get_user_model().objects.create_user(email='testuser@test.com', password='12345')
        self.thread = Thread.objects.create(name='Test Thread', user=self.user)

    def test_message_creation_requires_login(self):
        # Attempt to create a message without being logged in
        response = self.client.post(reverse('new_message', kwargs={'pk': self.thread.pk}), {'content': 'Hello, World!'})
        self.assertEqual(response.status_code, 302)  # Expect a redirect to the login page
        self.assertTrue(response.url.startswith(reverse('login')))  # Check the redirect URL
        self.assertFalse(Message.objects.filter(content='Hello, World!').exists())  # Message should not exist

    def tearDown(self):
        # Clean up after each test method
        self.client.logout()
