# aistarterkit/chat/tests/test_integration.py
from django.test import TestCase
from django.urls import reverse
from django.contrib.auth import get_user_model


class UserAuthenticationTestCase(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(email='testuser@test.com', password='12345')

    def test_login_logout(self):
        # Test login
        response = self.client.post(reverse('login'), {'username': 'testuser@test.com', 'password': '12345'})
        self.assertRedirects(response, reverse('settings'))

        # Test logout
        response = self.client.get(reverse('logout'))
        self.assertRedirects(response, reverse('login'))
