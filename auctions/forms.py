from django import forms
from django.contrib.auth.forms import UserCreationForm
from django.db import transaction

from .models import Profile


class SignupForm(UserCreationForm):
    role = forms.ChoiceField(
        choices=Profile.Role.choices,
        initial=Profile.Role.BIDDER,
        widget=forms.RadioSelect,
        help_text="Bidders place bids. Sellers create auctions.",
    )

    class Meta(UserCreationForm.Meta):
        fields = ("username",)

    def clean_username(self):
        username = super().clean_username()
        # Usernames are stored as the bidder on bids, which allow 64 characters.
        if len(username) > 64:
            raise forms.ValidationError("Use 64 characters or fewer.")
        return username

    @transaction.atomic
    def save(self, commit=True):
        user = super().save(commit=True)
        Profile.objects.create(user=user, role=self.cleaned_data["role"])
        return user
