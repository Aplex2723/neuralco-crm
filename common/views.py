import json
from multiprocessing import context
from re import template

import requests
from django.conf import settings
from django.contrib.auth import authenticate, login
from django.contrib.auth.hashers import make_password
from django.db import transaction
from django.db.models import Q
from django.http.response import JsonResponse
from django.shortcuts import get_object_or_404, render
from django.utils import timezone
from django.utils.encoding import force_str
from django.utils.http import urlsafe_base64_decode
from django.utils.translation import gettext as _
from django.views import View
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods
from drf_yasg.utils import swagger_auto_schema
from rest_framework import status
from rest_framework.authtoken.models import Token
from rest_framework.pagination import LimitOffsetPagination
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from accounts.models import Account, Contact, Tags
from accounts.serializer import AccountSerializer
from cases.models import Case
from cases.serializer import CaseSerializer
##from common.custom_auth import JSONWebTokenAuthentication
from common import serializer, swagger_params
from common.models import APISettings, Document, Org, Profile, User, Subscription
from common.serializer import *
from common.serializer import (CreateUserSerializer, PasswordChangeSerializer,
                               RegisterOrganizationSerializer)
from common.tasks import (resend_activation_link_to_user,
                          send_email_to_new_user, send_email_to_reset_password,
                          send_email_user_delete)
from common.token_generator import account_activation_token
# from rest_framework_jwt.serializers import jwt_encode_handler
from common.utils import COUNTRIES, ROLES, jwt_payload_handler, PLAN_CHOICES
from contacts.serializer import ContactSerializer
from leads.models import Lead
from leads.serializer import LeadSerializer
from opportunity.models import Opportunity
from opportunity.serializer import OpportunitySerializer
from teams.models import Teams
from teams.serializer import TeamsSerializer


class GetTeamsAndUsersView(APIView):

    ##authentication_classes = (JSONWebTokenAuthentication,)
    permission_classes = (IsAuthenticated,)

    @swagger_auto_schema(
        tags=["Users"], manual_parameters=swagger_params.organization_params
    )
    def get(self, request, *args, **kwargs):
        data = {}
        teams = Teams.objects.filter(org=request.org).order_by("-id")
        teams_data = TeamsSerializer(teams, many=True).data
        profiles = Profile.objects.filter(is_active=True, org=request.org).order_by(
            "user__email"
        )
        profiles_data = ProfileSerializer(profiles, many=True).data
        data["teams"] = teams_data
        data["profiles"] = profiles_data
        return Response(data)


class UserDetailView(APIView):
    ##authentication_classes = (JSONWebTokenAuthentication,)
    permission_classes = (IsAuthenticated,)

    def get_object(self, pk):
        profile = get_object_or_404(Profile, pk=pk)
        return profile

    @swagger_auto_schema(
        tags=["Users"], manual_parameters=swagger_params.organization_params
    )
    def get(self, request, pk, format=None):
        profile_obj = self.get_object(pk)

        profile=Profile.objects.get(user=request.user)
        if (
            profile.role != "ADMIN"
            and not profile.is_admin
            and profile.id != profile_obj.id
        ):
            return Response(
                {"error": True, "errors": "Permission Denied"},
                status=status.HTTP_403_FORBIDDEN,
            )

        if profile_obj.org.id != request.org.id:
            return Response(
                {"error": True, "errors": "User company doesnot match with header...."},
                status=status.HTTP_403_FORBIDDEN,
            )

        assigned_data = Profile.objects.filter(org=request.org, is_active=True).values(
            "id", "user__first_name"
        )
        context = {}
        context["profile_obj"] = ProfileSerializer(profile_obj).data
        opportunity_list = Opportunity.objects.filter(assigned_to=profile_obj)
        context["opportunity_list"] = OpportunitySerializer(
            opportunity_list, many=True
        ).data
        contacts = Contact.objects.filter(assigned_to=profile_obj)
        context["contacts"] = ContactSerializer(contacts, many=True).data
        cases = Case.objects.filter(assigned_to=profile_obj)
        context["cases"] = CaseSerializer(cases, many=True).data
        context["assigned_data"] = assigned_data
        comments = profile_obj.user_comments.all()
        context["comments"] = CommentSerializer(comments, many=True).data
        context["countries"] = COUNTRIES
        return Response(
            {"error": False, "data": context},
            status=status.HTTP_200_OK,
        )

    @swagger_auto_schema(
        tags=["Users"], manual_parameters=swagger_params.user_update_params
    )
    def put(self, request, pk, format=None):
        print(request)
        params = request.post_data
        profile = self.get_object(pk)
        address_obj = profile.address
        profile=Profile.objects.get(user=request.user)
        if (
            profile.role != "ADMIN"
            and not user.is_superuser
            and profile.id != profile.id
        ):
            return Response(
                {"error": True, "errors": "Permission Denied"},
                status=status.HTTP_403_FORBIDDEN,
            )

        if profile.org.id != request.org.id:
            return Response(
                {"error": True, "errors": "User company doesnot match with header...."},
                status=status.HTTP_403_FORBIDDEN,
            )
        serializer = CreateUserSerializer(
            data=params, instance=profile.user, org=request.org
        )
        address_serializer = BillingAddressSerializer(data=params, instance=address_obj)
        profile_serializer = CreateProfileSerializer(data=params, instance=profile)
        data = {}
        if not serializer.is_valid():
            data["contact_errors"] = serializer.errors
        if not address_serializer.is_valid():
            data["address_errors"] = (address_serializer.errors,)
        if not profile_serializer.is_valid():
            data["profile_errors"] = (profile_serializer.errors,)
        if data:
            data["error"] = True
            return Response(
                data,
                status=status.HTTP_400_BAD_REQUEST,
            )
        if serializer.is_valid():
            address_obj = address_serializer.save()
            user = serializer.save()
            user.username = user.first_name
            user.save()
            profile = profile_serializer.save()
            return Response(
                {"error": False, "message": "User Updated Successfully"},
                status=status.HTTP_200_OK,
            )
        return Response(
            {"error": True, "errors": serializer.errors},
            status=status.HTTP_400_BAD_REQUEST,
        )

    @swagger_auto_schema(
        tags=["Users"], manual_parameters=swagger_params.organization_params
    )
    def delete(self, request, pk, format=None):
        profile=Profile.objects.get(user=request.user)
        if profile.role != "ADMIN" and not profile.is_admin:
            return Response(
                {"error": True, "errors": "Permission Denied"},
                status=status.HTTP_403_FORBIDDEN,
            )
        self.object = self.get_object(pk)
        if self.object.id == request.profile.id:
            return Response(
                {"error": True, "errors": "Permission Denied"},
                status=status.HTTP_403_FORBIDDEN,
            )
        deleted_by = profile.user.email
        send_email_user_delete.delay(
            self.object.user.email,
            deleted_by=deleted_by,
        )
        self.object.delete()
        return Response({"status": "success"}, status=status.HTTP_200_OK)


class ChangePasswordView(APIView):
    ##authentication_classes = (JSONWebTokenAuthentication,)
    permission_classes = (IsAuthenticated,)

    @swagger_auto_schema(
        tags=["Profile"],
        operation_description="This is change password api",
        manual_parameters=swagger_params.change_password_params,
    )
    def post(self, request, format=None):
        params = request.post_data
        context = {"user": request.user}
        serializer = PasswordChangeSerializer(data=params, context=context)
        if serializer.is_valid():
            user = request.user
            user.set_password(params.get("new_password"))
            user.save()
            return Response(
                {"error": False, "message": "Password Changed Successfully"},
                status=status.HTTP_200_OK,
            )
        return Response(
            {"error": True, "errors": serializer.errors},
            status=status.HTTP_400_BAD_REQUEST,
        )


# check_header not working
class ApiHomeView(APIView):

    ##authentication_classes = (JSONWebTokenAuthentication,)
    permission_classes = (IsAuthenticated,)

    @swagger_auto_schema(
        tags=["dashboard"], manual_parameters=swagger_params.organization_params
    )
    def get(self, request, format=None):
        accounts = Account.objects.filter(status="open", org=request.org)
        contacts = Contact.objects.filter(org=request.org)
        leads = Lead.objects.filter(org=request.org).exclude(
            Q(status="converted") | Q(status="closed")
        )
        opportunities = Opportunity.objects.filter(org=request.org)
        profile=Profile.objects.get(user=request.user)

        if profile.role != "ADMIN" and not self.request.user.is_superuser:
            accounts = accounts.filter(
                Q(assigned_to=profile) | Q(created_by=profile)
            )
            contacts = contacts.filter(
                Q(assigned_to__id__in=profile)
                | Q(created_by=profile)
            )
            leads = leads.filter(
                Q(assigned_to__id__in=profile)
                | Q(created_by=profile)
            ).exclude(status="closed")
            opportunities = opportunities.filter(
                Q(assigned_to__id__in=profile)
                | Q(created_by=profile)
            )
        context = {}
        context["accounts_count"] = accounts.count()
        context["contacts_count"] = contacts.count()
        context["leads_count"] = leads.count()
        context["opportunities_count"] = opportunities.count()
        context["accounts"] = AccountSerializer(accounts, many=True).data
        context["contacts"] = ContactSerializer(contacts, many=True).data
        context["leads"] = LeadSerializer(leads, many=True).data
        context["opportunities"] = OpportunitySerializer(opportunities, many=True).data
        return Response(context, status=status.HTTP_200_OK)

class SubscriptionView(APIView):
    permission_classes = (IsAuthenticated,)
    serializer_class = SubcriptionSerializer

    def post(self, request):

        if not request.org:
            return Response({
                "error": True,
                "errors": ["The ORG is required for a subscription"]
            }, status=status.HTTP_400_BAD_REQUEST)

        serializer = self.serializer_class(data=request.post_data) 
        if serializer.is_valid():
            params = request.post_data

            exist_subscription = Subscription.objects.filter(
                user=request.user,
            ).first()

            org_obj = Org.objects.filter(pk=request.org.id).first()
            if params.get('plan') == 'basic':
                org_obj.user_limit = 2 
            elif params.get('plan') == 'entrepreneur':
                org_obj.user_limit = 100 
            elif params.get('plan') == 'pro':
                org_obj.user_limit = 1000 
            elif params.get('plan') == 'corporate':
                org_obj.user_limit = 3000
            elif params.get('plan') == 'custom':
                org_obj.user_limit = 10000                            
            org_obj.save()

            if exist_subscription:

                exist_subscription.plan = params.get('plan')
                exist_subscription.start_date = timezone.now()
                exist_subscription.save()

                return Response({
                    "error": False,
                    "msg": "Success on subscription update",
                    "status": status.HTTP_200_OK
                }, status=status.HTTP_200_OK)
            else:
                Subscription.objects.create(
                    user=request.user,
                    plan=params.get('plan') or 'basic',
                    is_annual=params.get('is_annual') or False
                )
                return Response({
                    "error": False,
                    "msg": "Success on subscription creation",
                    "status": status.HTTP_201_CREATED
                }, status=status.HTTP_201_CREATED)

        else:
            return Response({
                "error": True,
                "errors": serializer.errors,
                "status": status.HTTP_400_BAD_REQUEST
            }, status=status.HTTP_400_BAD_REQUEST)

    def get(self, request):
        try:
            user_subscription = Subscription.objects.get(user=request.user)

            
            context = {}

            if request.org:
                context['user_limit'] = request.org.user_limit
            
            context["plan"] = user_subscription.plan
            context["is_active"] = user_subscription.is_active()
            context["is_annual"] = user_subscription.is_annual
            context["start_date"] = user_subscription.start_date
            context["end_date"] = user_subscription.end_date

            return Response({
                "error": False,
                "data": context
            })
        except:
            return Response({
                "error": True,
                "errors": ["User not found in Subscriptions"]
            })

class LoginView(APIView):
    serializer_class = LoginSealizer

    @swagger_auto_schema(
        tags=["Auth"],
        operation_description="This is login api",
        manual_parameters=swagger_params.login_page_params,
    )
    def post(self, request):
        try:
            user = User.objects.get(email=request.post_data.get("email"))
            if not user.is_active:
                context = {
                    "error": True,
                    "errors": "The user is not active"
                }
                return Response(context)
                
        except User.DoesNotExist:
            pass

        serializer = self.serializer_class(data=request.post_data)  #<------
        serializer.is_valid(raise_exception=True)
        
        context = {
                "error": False,
                "status": status.HTTP_200_OK,
                "tokens": serializer.data,
            }
        return Response(
            context
        )


class RegistrationView(APIView):
    model = User
    subscription_model = Subscription
    serializer_class = RegisterOrganizationSerializer

    @swagger_auto_schema(
        tags=["Auth"],
        operation_description="This is registration api",
        manual_parameters=swagger_params.registration_page_params,
    )
    def post(self, request, format=None):

        serializer = self.serializer_class(data=request.post_data)

        if serializer.is_valid():
            user_obj = serializer.save()
            subscription = self.subscription_model.objects.create(
                user=user_obj,
            )
            print(subscription)
            subscription.save()
            password = request.post_data.get("password")
            user_obj.password = make_password(password)
            user_obj.save()


            # sending mail for confirm password
            # if this cause an error, please configure the Email fields and Celery fields on the .env
            # remember keep the Celery server on with the following command: celery -A crm worker --loglevel=info
            send_email_to_new_user.delay(
                user_obj.id,
            )


            return Response({"error": False, "status": status.HTTP_201_CREATED})
        else:
            return Response(
                {
                    "error": True,
                    "errors": serializer.errors,
                    "status": status.HTTP_400_BAD_REQUEST,
                }
            )


class OrgProfileCreateView(APIView):
    ##authentication_classes = (JSONWebTokenAuthentication,)
    permission_classes = (IsAuthenticated,)

    model1 = Org
    model2 = Profile
    serializer_class = OrgProfileCreateSerializer
    profile_serializer = CreateProfileSerializer

    @swagger_auto_schema(
        tags=["Auth"],
        operation_description="This is registration api",
        manual_parameters=swagger_params.post_org_creation_page_params,
    )
    def post(self, request, format=None):
        params = request.post_data

        serializer = self.serializer_class(data=request.post_data)
        if serializer.is_valid():
            org_obj = serializer.save()

            # now creating the profile
            profile_obj = self.model2.objects.create(user=request.user, org=org_obj)
            # now the current user is the admin of the newly created organisation
            profile_obj.is_organization_admin = True
            profile_obj.role = 'ADMIN'
            profile_obj.save()

            return Response(
                {
                    "error": False,
                    "message": "New Org is Created.",
                    "org": self.serializer_class(org_obj).data,
                    "status": status.HTTP_201_CREATED,
                }
            )
        else:
            return Response(
                {
                    "error": True,
                    "errors": serializer.errors,
                    "status": status.HTTP_400_BAD_REQUEST,
                }
            )

    @swagger_auto_schema(
        tags=["Auth"],
        operation_description="Just Pass the token, will return ORG list, associated with the user.",
        # manual_parameters=swagger_params.organization_params,
    )
    def get(self, request, format=None):
        """
        here we are passing profile list of the user, where org details also included
        """
        profile_list = Profile.objects.filter(user=request.user)
        serializer = ShowOrganizationListSerializer(profile_list, many=True)
        return Response(
            {
                "error": False,
                "status": status.HTTP_200_OK,
                "profile_org_list": serializer.data,
            }
        )


class ProfileView(APIView):
    # ##authentication_classes = (JSONWebTokenAuthentication,
    permission_classes = (IsAuthenticated,)

    @swagger_auto_schema(
        tags=["Profile"], manual_parameters=swagger_params.organization_params
    )
    def get(self, request, format=None):
        # profile=Profile.objects.get(user=request.user)
        context = {}
        context["user_obj"] = ProfileSerializer(self.request.profile).data
        return Response(context, status=status.HTTP_200_OK)


class UsersListView(APIView, LimitOffsetPagination):

    ##authentication_classes = (JSONWebTokenAuthentication,)
    permission_classes = (IsAuthenticated,)

    @swagger_auto_schema(
        tags=["Users"], manual_parameters=swagger_params.user_create_params
    )
    def post(self, request, format=None):
        profile=Profile.objects.get(user=request.user)
        if profile.role != "ADMIN" and not request.user.is_superuser:
            return Response(
                {"error": True, "errors": "Permission Denied"},
                status=status.HTTP_403_FORBIDDEN,
            )
        else:
            params = request.post_data
            if params:
                user_serializer = CreateUserSerializer(data=params, org=request.org)
                address_serializer = BillingAddressSerializer(data=params)
                profile_serializer = CreateProfileSerializer(data=params)
                data = {}
                if not user_serializer.is_valid():
                    data["user_errors"] = dict(user_serializer.errors)
                if not profile_serializer.is_valid():
                    data["profile_errors"] = profile_serializer.errors
                if not address_serializer.is_valid():
                    data["address_errors"] = (address_serializer.errors,)
                if data:
                    return Response(
                        {"error": True, "errors": data},
                        status=status.HTTP_400_BAD_REQUEST,
                    )
                if user_serializer.is_valid():
                    address_obj = address_serializer.save()
                    user = user_serializer.save(
                        is_active=False,
                    )
                    user.username = user.first_name
                    user.save()
                    if params.get("password"):
                        user.set_password(params.get("password"))
                        user.save()
                    profile = Profile.objects.create(
                        user=user,
                        date_of_joining=timezone.now(),
                        role=params.get("role"),
                        address=address_obj,
                        org=request.org,
                    )

                    send_email_to_new_user.delay(
                        user.id,
                    )
                    return Response(
                        {"error": False, "message": "User Created Successfully"},
                        status=status.HTTP_201_CREATED,
                    )

    @swagger_auto_schema(
        tags=["Users"], manual_parameters=swagger_params.user_list_params
    )
    def get(self, request, format=None):
        profile=Profile.objects.get(user=request.user)
        if profile.role != "ADMIN" and not request.user.is_superuser:
            return Response(
                {"error": True, "errors": "Permission Denied"},
                status=status.HTTP_403_FORBIDDEN,
            )
        params = request.post_data
        queryset = Profile.objects.filter(org=request.org).order_by("-id")
        if params:
            if params.get("email"):
                queryset = queryset.filter(user__email__icontains=params.get("email"))
            if params.get("role"):
                queryset = queryset.filter(role=params.get("role"))
            if params.get("status"):
                queryset = queryset.filter(is_active=params.get("status"))

        context = {}
        queryset_active_users = queryset.filter(is_active=True)
        results_active_users = self.paginate_queryset(
            queryset_active_users.distinct(), self.request, view=self
        )
        active_users = ProfileSerializer(results_active_users, many=True).data
        if results_active_users:
            offset = queryset_active_users.filter(
                id__gte=results_active_users[-1].id
            ).count()
            if offset == queryset_active_users.count():
                offset = None
        else:
            offset = 0
        context["active_users"] = {
            "active_users_count": self.count,
            "active_users": active_users,
            "offset": offset,
        }

        queryset_inactive_users = queryset.filter(is_active=False)
        results_inactive_users = self.paginate_queryset(
            queryset_inactive_users.distinct(), self.request, view=self
        )
        inactive_users = ProfileSerializer(results_inactive_users, many=True).data
        if results_inactive_users:
            offset = queryset_inactive_users.filter(
                id__gte=results_inactive_users[-1].id
            ).count()
            if offset == queryset_inactive_users.count():
                offset = None
        else:
            offset = 0
        context["inactive_users"] = {
            "inactive_users_count": self.count,
            "inactive_users": inactive_users,
            "offset": offset,
        }

        context["admin_email"] = settings.ADMIN_EMAIL
        context["roles"] = ROLES
        context["status"] = [("True", "Active"), ("False", "In Active")]
        return Response(context)


class DocumentListView(APIView, LimitOffsetPagination):
    ##authentication_classes = (JSONWebTokenAuthentication,)
    permission_classes = (IsAuthenticated,)
    model = Document

    def get_context_data(self, request, **kwargs):
        params = request.post_data
        queryset = self.model.objects.filter(org=request.org).order_by("-id")
        profile=Profile.objects.get(user=request.user)
        if self.request.user.is_superuser or profile.role == "ADMIN":
            queryset = queryset
        else:
            if profile.documents():
                doc_ids = profile.documents().values_list("id", flat=True)
                shared_ids = queryset.filter(
                    Q(status="active") & Q(shared_to__id__in=[profile.id])
                ).values_list("id", flat=True)
                queryset = queryset.filter(Q(id__in=doc_ids) | Q(id__in=shared_ids))
            else:
                queryset = queryset.filter(
                    Q(status="active") & Q(shared_to__id__in=[profile.id])
                )

        request_post = params
        if request_post:
            if request_post.get("title"):
                queryset = queryset.filter(title__icontains=request_post.get("title"))
            if request_post.get("status"):
                queryset = queryset.filter(status=request_post.get("status"))

            if request_post.get("shared_to"):
                queryset = queryset.filter(
                    shared_to__id__in=json.loads(request_post.get("shared_to"))
                )

        context = {}
        profile_list = Profile.objects.filter(is_active=True, org=request.org)
        profile=Profile.objects.get(user=request.user)
        if profile.role == "ADMIN" or profile.is_admin:
            profiles = profile_list.order_by("user__email")
        else:
            profiles = profile_list.filter(role="ADMIN").order_by("user__email")
        search = False
        if (
            params.get("document_file")
            or params.get("status")
            or params.get("shared_to")
        ):
            search = True
        context["search"] = search

        queryset_documents_active = queryset.filter(status="active")
        results_documents_active = self.paginate_queryset(
            queryset_documents_active.distinct(), self.request, view=self
        )
        documents_active = DocumentSerializer(results_documents_active, many=True).data
        if results_documents_active:
            offset = queryset_documents_active.filter(
                id__gte=results_documents_active[-1].id
            ).count()
            if offset == queryset_documents_active.count():
                offset = None
        else:
            offset = 0
        context["documents_active"] = {
            "documents_active_count": self.count,
            "documents_active": documents_active,
            "offset": offset,
        }

        queryset_documents_inactive = queryset.filter(status="inactive")
        results_documents_inactive = self.paginate_queryset(
            queryset_documents_inactive.distinct(), self.request, view=self
        )
        documents_inactive = DocumentSerializer(
            results_documents_inactive, many=True
        ).data
        if results_documents_inactive:
            offset = queryset_documents_inactive.filter(
                id__gte=results_documents_active[-1].id
            ).count()
            if offset == queryset_documents_inactive.count():
                offset = None
        else:
            offset = 0
        context["documents_inactive"] = {
            "documents_inactive_count": self.count,
            "documents_inactive": documents_inactive,
            "offset": offset,
        }

        context["users"] = ProfileSerializer(profiles, many=True).data
        context["status_choices"] = Document.DOCUMENT_STATUS_CHOICE
        return context

    @swagger_auto_schema(
        tags=["documents"], manual_parameters=swagger_params.document_get_params
    )
    def get(self, request, *args, **kwargs):
        context = self.get_context_data(**kwargs)
        return Response(context)

    @swagger_auto_schema(
        tags=["documents"], manual_parameters=swagger_params.document_create_params
    )
    def post(self, request, *args, **kwargs):
        params = request.post_data
        serializer = DocumentCreateSerializer(data=params, request_obj=request)
        if serializer.is_valid():
            doc = serializer.save(
                created_by=request.profile,
                org=request.org,
                document_file=request.FILES.get("document_file"),
            )
            if params.get("shared_to"):
                assinged_to_list = json.loads(params.get("shared_to"))
                profiles = Profile.objects.filter(
                    id__in=assinged_to_list, org=request.org, is_active=True
                )
                if profiles:
                    doc.shared_to.add(*profiles)
            if params.get("teams"):
                teams_list = json.loads(params.get("teams"))
                teams = Teams.objects.filter(id__in=teams_list, org=request.org)
                if teams:
                    doc.teams.add(*teams)

            return Response(
                {"error": False, "message": "Document Created Successfully"},
                status=status.HTTP_201_CREATED,
            )
        return Response(
            {"error": True, "errors": serializer.errors},
            status=status.HTTP_400_BAD_REQUEST,
        )


class DocumentDetailView(APIView):
    ##authentication_classes = (JSONWebTokenAuthentication,)
    permission_classes = (IsAuthenticated,)

    def get_object(self, pk):
        return Document.objects.filter(id=pk).first()

    @swagger_auto_schema(
        tags=["documents"], manual_parameters=swagger_params.organization_params
    )
    def get(self, request, pk, format=None):
        self.object = self.get_object(pk)

        profile=Profile.objects.get(user=request.user)

        if not self.object:
            return Response(
                {"error": True, "errors": "Document does not exist"},
                status=status.HTTP_403_FORBIDDEN,
            )
        if self.object.org != request.org:
            return Response(
                {"error": True, "errors": "User company doesnot match with header...."},
                status=status.HTTP_403_FORBIDDEN,
            )
        if profile.role != "ADMIN" and not self.request.user.is_superuser:
            if not (
                (profile == self.object.created_by)
                or (profile in self.object.shared_to.all())
            ):
                return Response(
                    {
                        "error": True,
                        "errors": "You do not have Permission to perform this action",
                    },
                    status=status.HTTP_403_FORBIDDEN,
                )
        profile_list = Profile.objects.filter(org=request.org)
        if request.profile.role == "ADMIN" or request.user.is_superuser:
            profiles = profile_list.order_by("user__email")
        else:
            profiles = profile_list.filter(role="ADMIN").order_by("user__email")
        context = {}
        context.update(
            {
                "doc_obj": DocumentSerializer(self.object).data,
                "file_type_code": self.object.file_type()[1],
                "users": ProfileSerializer(profiles, many=True).data,
            }
        )
        return Response(context, status=status.HTTP_200_OK)

    @swagger_auto_schema(
        tags=["documents"], manual_parameters=swagger_params.organization_params
    )
    def delete(self, request, pk, format=None):

        profile=Profile.objects.get(user=request.user)
        document = self.get_object(pk)
        if not document:
            return Response(
                {"error": True, "errors": "Documdnt does not exist"},
                status=status.HTTP_403_FORBIDDEN,
            )
        if document.org != request.org:
            return Response(
                {"error": True, "errors": "User company doesnot match with header...."},
                status=status.HTTP_403_FORBIDDEN,
            )

        if profile.role != "ADMIN" and not self.request.user.is_superuser:
            if (
                profile != document.created_by
            ):  # or (profile not in document.shared_to.all()):
                return Response(
                    {
                        "error": True,
                        "errors": "You do not have Permission to perform this action",
                    },
                    status=status.HTTP_403_FORBIDDEN,
                )
        document.delete()
        return Response(
            {"error": False, "message": "Document deleted Successfully"},
            status=status.HTTP_200_OK,
        )

    @swagger_auto_schema(
        tags=["documents"], manual_parameters=swagger_params.document_update_params
    )
    def put(self, request, pk, format=None):

        self.object = self.get_object(pk)
        params = request.post_data
        profile=Profile.objects.get(user=request.user)
        if not self.object:
            return Response(
                {"error": True, "errors": "Document does not exist"},
                status=status.HTTP_403_FORBIDDEN,
            )
        if self.object.org != request.org:
            return Response(
                {"error": True, "errors": "User company doesnot match with header...."},
                status=status.HTTP_403_FORBIDDEN,
            )
        if profile.role != "ADMIN" and not self.request.user.is_superuser:
            if not (
                (profile == self.object.created_by)
                or (profile in self.object.shared_to.all())
            ):
                return Response(
                    {
                        "error": True,
                        "errors": "You do not have Permission to perform this action",
                    },
                    status=status.HTTP_403_FORBIDDEN,
                )
        serializer = DocumentCreateSerializer(
            data=params, instance=self.object, request_obj=request
        )
        if serializer.is_valid():
            doc = serializer.save(
                document_file=request.FILES.get("document_file"),
                status=params.get("status"),
                org=request.org,
            )
            doc.shared_to.clear()
            if params.get("shared_to"):
                assinged_to_list = json.loads(params.get("shared_to"))
                profiles = Profile.objects.filter(
                    id__in=assinged_to_list, org=request.org, is_active=True
                )
                if profiles:
                    doc.shared_to.add(*profiles)

            doc.teams.clear()
            if params.get("teams"):
                teams_list = json.loads(params.get("teams"))
                teams = Teams.objects.filter(id__in=teams_list, org=request.org)
                if teams:
                    doc.teams.add(*teams)
            return Response(
                {"error": False, "message": "Document Updated Successfully"},
                status=status.HTTP_200_OK,
            )
        return Response(
            {"error": True, "errors": serializer.errors},
            status=status.HTTP_400_BAD_REQUEST,
        )


class ForgotPasswordView(APIView):
    @swagger_auto_schema(
        tags=["Auth"], manual_parameters=swagger_params.forgot_password_params
    )
    def post(self, request, format=None):
        params = request.post_data
        serializer = ForgotPasswordSerializer(data=params)
        if serializer.is_valid():
            user = get_object_or_404(User, email=params.get("email"))
            if not user.is_active:
                return Response(
                    {"error": True, "errors": "Please activate account to proceed."},
                    status=status.HTTP_406_NOT_ACCEPTABLE,
                )
            send_email_to_reset_password.delay(user.email)
            data = {
                "error": False,
                "message": "We have sent you an email. please reset password",
            }
            return Response(data, status=status.HTTP_200_OK)
        else:

            error = serializer.errors.get("non_field_errors")

            data = {"error": True, "errors": serializer.errors, "error_text": error[0]}
            response_status = status.HTTP_400_BAD_REQUEST
            return Response(data, status=response_status)


class ResetPasswordView(APIView):
    @swagger_auto_schema(
        tags=["Auth"], manual_parameters=swagger_params.reset_password_params
    )
    def post(self, request, uid, token, format=None):
        params = request.post_data
        try:
            uid = force_str(urlsafe_base64_decode(uid))
            user_obj = User.objects.get(pk=uid)
            if not user_obj.password:
                if not user_obj.is_active:
                    user_obj.is_active = True
                    user_obj.save()
        except (TypeError, ValueError, OverflowError, User.DoesNotExist):
            user_obj = None
        if user_obj is not None:
            password1 = params.get("new_password1")
            password2 = params.get("new_password2")
            if password1 != password2:
                return Response(
                    {"error": True, "errors": "The two password fields didn't match."},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            else:
                user_obj.set_password(password1)
                user_obj.save()
                return Response(
                    {
                        "error": False,
                        "message": "Password Updated Successfully. Please login",
                    },
                    status=status.HTTP_200_OK,
                )
        else:
            return Response({"error": True, "errors": "Invalid Link"})


class UserStatusView(APIView):
    ##authentication_classes = (JSONWebTokenAuthentication,)
    permission_classes = (IsAuthenticated,)

    @swagger_auto_schema(
        tags=["Users"], manual_parameters=swagger_params.users_status_params
    )
    def post(self, request, pk, format=None):

        profile=Profile.objects.get(user=request.user)
        if profile.role != "ADMIN" and not self.request.user.is_superuser:
            return Response(
                {
                    "error": True,
                    "errors": "You do not have permission to perform this action",
                },
                status=status.HTTP_403_FORBIDDEN,
            )
        params = request.post_data
        profiles = Profile.objects.filter(org=request.org)
        profile = profiles.get(id=pk)
        if request.user.id == profile.user_id:
            return Response({
                "error": True,
                "errors": "You cant manage your own email",
            }, status=status.HTTP_401_UNAUTHORIZED)

        if params.get("status"):
            user_status = params.get("status")
            if user_status == "Active":
                profile.is_active = True
            elif user_status == "Inactive":
                profile.is_active = False
            else:
                return Response(
                    {"error": True, "errors": "Please enter Valid Status for user"},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            profile.save()

        context = {}
        active_profiles = profiles.filter(is_active=True)
        inactive_profiles = profiles.filter(is_active=False)
        context["active_profiles"] = ProfileSerializer(active_profiles, many=True).data
        context["inactive_profiles"] = ProfileSerializer(
            inactive_profiles, many=True
        ).data
        return Response(context)


class DomainList(APIView):
    model = APISettings
    ##authentication_classes = (JSONWebTokenAuthentication,)
    permission_classes = (IsAuthenticated,)

    @swagger_auto_schema(
        tags=["Settings"], manual_parameters=swagger_params.organization_params
    )
    def get(self, request, *args, **kwargs):
        api_settings = APISettings.objects.filter(org=request.org)
        users = Profile.objects.filter(is_active=True, org=request.org).order_by(
            "user__email"
        )
        return Response(
            {
                "error": False,
                "api_settings": APISettingsListSerializer(api_settings, many=True).data,
                "users": ProfileSerializer(users, many=True).data,
            },
            status=status.HTTP_200_OK,
        )

    @swagger_auto_schema(
        tags=["Settings"], manual_parameters=swagger_params.api_setting_create_params
    )
    def post(self, request, *args, **kwargs):
        params = request.post_data
        assigned_lead = None
        if params.get("lead_assigned_to"):
            try:
                assigned_lead = Profile.objects.get(pk=params.get("lead_assigned_to"))
            except:
                return Response(
                    {"error": True, "message": "Invalid lead_assiged_id"},
                    status=status.HTTP_201_CREATED,
                )
        serializer = APISettingsSerializer(data=params)
        if serializer.is_valid():
            profile=Profile.objects.get(user=request.user)
            org=Org.objects.get(pk=request.org.id)
            settings_obj = serializer.save(created_by=profile, org=org)
            if params.get("tags"):
                tags = params.get("tags")
                for tag in tags:
                    tag_obj = Tags.objects.filter(name=tag).first()
                    if not tag_obj:
                        tag_obj = Tags.objects.create(name=tag)
                    settings_obj.tags.add(tag_obj)
            if assigned_lead:
                settings_obj.lead_assigned_to.add(assigned_lead)
            return Response(
                {"error": False, "message": "API key added sucessfully"},
                status=status.HTTP_201_CREATED,
            )
        return Response(
            {"error": True, "errors": serializer.errors},
            status=status.HTTP_400_BAD_REQUEST,
        )


class DomainDetailView(APIView):
    model = APISettings
    ##authentication_classes = (JSONWebTokenAuthentication,)
    permission_classes = (IsAuthenticated,)

    @swagger_auto_schema(
        tags=["Settings"], manual_parameters=swagger_params.organization_params
    )
    def get(self, request, pk, format=None):
        api_setting = self.get_object(pk)
        return Response(
            {"error": False, "domain": APISettingsListSerializer(api_setting).data},
            status=status.HTTP_200_OK,
        )

    @swagger_auto_schema(
        tags=["Settings"], manual_parameters=swagger_params.api_setting_create_params
    )
    def put(self, request, pk, **kwargs):
        api_setting = self.get_object(pk)
        params = request.post_data
        assign_to_list = []
        if params.get("lead_assigned_to"):
            assign_to_list = json.loads(params.get("lead_assigned_to"))
        serializer = APISettingsSerializer(data=params, instance=api_setting)
        if serializer.is_valid():
            api_setting = serializer.save()
            api_setting.tags.clear()
            api_setting.lead_assigned_to.clear()
            if params.get("tags"):
                tags = json.loads(params.get("tags"))
                for tag in tags:
                    tag_obj = Tags.objects.filter(name=tag).first()
                    if not tag_obj:
                        tag_obj = Tags.objects.create(name=tag)
                    api_setting.tags.add(tag_obj)
            if assign_to_list:
                api_setting.lead_assigned_to.add(*assign_to_list)
            return Response(
                {"error": False, "message": "API setting Updated sucessfully"},
                status=status.HTTP_200_OK,
            )
        return Response(
            {"error": True, "errors": serializer.errors},
            status=status.HTTP_400_BAD_REQUEST,
        )

    @swagger_auto_schema(
        tags=["Settings"], manual_parameters=swagger_params.organization_params
    )
    def delete(self, request, pk, **kwargs):
        api_setting = self.get_object(pk)
        if api_setting:
            api_setting.delete()
        return Response(
            {"error": False, "message": "API setting deleted sucessfully"},
            status=status.HTTP_200_OK,
        )


class ActivateUserView(View):
    template = "common/user_activation_status.html"
    # @swagger_auto_schema(
    #     tags=["Auth"],
    # )
    def get(self, request, uid, token, activation_key, format=None):
        user = User.objects.get(activation_key=activation_key)
        if user:
            if timezone.now() > user.key_expires:
                resend_activation_link_to_user.delay(
                    user.email,
                )
                context = {
                    "success": False,
                    "message": "Link expired. Please use the Activation link sent now to your mail.",
                }
                return render(request, self.template, context)
            else:
                try:
                    uid = force_str(urlsafe_base64_decode(uid))
                    user = User.objects.get(pk=uid)
                except (TypeError, ValueError, OverflowError, User.DoesNotExist):
                    user = None
                if user is not None and account_activation_token.check_token(
                    user, token
                ):
                    user.is_active = True
                    user.save()

                    context = {
                        "success": True,
                        "message": "Thank you for your email confirmation. Now you can login to your account.",
                    }
                    return render(request, self.template, context)

                context = {"success": False, "message": "In Valid Token."}
                return render(request, self.template, context)

    # def post(self, request, uid, token, activation_key, format=None):
    #     profile = get_object_or_404(Profile, activation_key=activation_key)
    #     if profile.user:
    #         if timezone.now() > profile.key_expires:
    #             resend_activation_link_to_user.delay(
    #                 profile.user.email,
    #             )
    #             return Response(
    #                 {
    #                     "error": False,
    #                     "message": "Link expired. Please use the Activation link sent now to your mail.",
    #                 },
    #                 status=status.HTTP_406_NOT_ACCEPTABLE,
    #             )
    #         else:
    #             try:
    #                 uid = force_str(urlsafe_base64_decode(uid))
    #                 user = User.objects.get(pk=uid)
    #             except (TypeError, ValueError, OverflowError, User.DoesNotExist):
    #                 user = None
    #             if user is not None and account_activation_token.check_token(
    #                 user, token
    #             ):
    #                 user.is_active = True
    #                 user.save()
    #                 return Response(
    #                     {
    #                         "error": False,
    #                         "message": "Thank you for your email confirmation. Now you can login to your account.",
    #                     },
    #                     status=status.HTTP_200_OK,
    #                 )
    #             return Response(
    #                 {"error": True, "errors": "Activation link is invalid!"},
    #                 status=status.HTTP_400_BAD_REQUEST,
    #             )


class ResendActivationLinkView(APIView):
    @swagger_auto_schema(
        tags=["Auth"], manual_parameters=swagger_params.forgot_password_params
    )
    def post(self, request, format=None):
        params = request.post_data
        user = get_object_or_404(User, email=params.get("email"))
        if user.is_active:
            return Response(
                {"error": True, "message": "Account is active. Please login"},
                status=status.HTTP_200_OK,
            )
        resend_activation_link_to_user.delay(
            user.email,
        )
        data = {
            "error": False,
            "message": "Please use the Activation link sent to your mail to activate account.",
        }
        return Response(data, status=status.HTTP_200_OK)


# class OrganizationListView(APIView, LimitOffsetPagination):

#     # ##authentication_classes = (JSONWebTokenAuthentication,)
#     permission_classes = (IsAuthenticated,)

#     @swagger_auto_schema(tags=["Auth"])
#     def get(self, request):
#         profiles = Profile.objects.filter(user=request.user, is_active=True)
#         companies = Org.objects.filter(id__in=profiles.values_list("org", flat=True))
#         return Response(
#             {
#                 "error": False,
#                 "companies": OrganizationSerializer(companies, many=True).data,
#             },
#             status=status.HTTP_200_OK,
#         )


# @require_http_methods(["POST"])
# @csrf_exempt
class GoogleLoginView(APIView):
    """
    Check for authentication with google
    post:
        Returns token of logged In user
    """

    @swagger_auto_schema(
        tags=["Auth"],
    )
    def post(self, request):

        form = SocialLoginSerializer(data=request.POST)
        if form.is_valid():
            params = {"access_token": request.POST.get("accessToken")}
            url = "https://www.googleapis.com/oauth2/v1/userinfo"
            kw = dict(params=params, headers={}, timeout=60)
            response = requests.request("GET", url, **kw)
            if response.status_code == 200:
                email_matches = User.objects.filter(email=response.json().get("email"))
                if email_matches:
                    user = email_matches.first()
                    # user = authenticate(email=user.email)
                    login(request, user)

                    payload = jwt_payload_handler(user)
                    response_data = {
                        "token": jwt_encode_handler(payload),
                        "error": False,
                        "id": user.id,
                        "employee_name": user.get_full_name(),
                    }
                    return JsonResponse(response_data, status=status.HTTP_200_OK)
                return JsonResponse(
                    {"error": True, "message": "Email not valid"},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            return JsonResponse(
                {"error": True, "message": "Email not valid"},
                status=status.HTTP_400_BAD_REQUEST,
            )
        return JsonResponse(
            {"error": True, "errors": form.errors}, status=status.HTTP_200_OK
        )