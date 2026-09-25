"""Federal Register / BIS rule ingestion (semigraph.ingestion.federal_register).

No network: pages come from an injected ``fetch(url) -> dict``. The classifier
fixtures are REAL rule titles and abstracts (Federal Register documents are US
public-domain text) fetched read-only from the live API on 2026-09-25; expected
kinds were chosen by reading each abstract, and every judgment call is marked.

Background: the v1 term query ``semiconductor OR "advanced computing" OR
"export controls"`` behaved as AND (14 hits, while the terms alone return
43/24/79 and 166 BIS RULEs exist since 2022-01-01) — so v1 stored 13 rules.
The fix under test fetches every BIS RULE and classifies locally.
"""

import json
import urllib.error
from datetime import date, datetime

import pytest

from semigraph.config import Settings
from semigraph.ingestion import federal_register as FR
from semigraph.ingestion.federal_register import RELEVANT_KINDS, RULE_KINDS, classify_rule

# document_number -> (title, abstract or None), verbatim from the live API
REAL_RULES: dict[str, tuple[str, str | None]] = {'2025-19001': ('Expansion of End-User Controls To Cover Affiliates of Certain Listed Entities',
                'In this interim final rule (IFR), the Bureau of Industry and Security (BIS) '
                'amends the Export Administration Regulations (EAR) to address diversion concerns '
                'involving entities on the Entity List and certain other restricted end users. '
                'Under this IFR, any entity that is at least 50 percent owned by one or more '
                'entities on the Entity List will itself automatically be subject to Entity List '
                'restrictions. This is a marked improvement over the current standard, which '
                'excludes all entities that are not specifically included on the Entity List, '
                'regardless of affiliation with Entity List entities. This IFR similarly applies '
                "restrictions to entities at least 50 percent owned by listed `military end users' "
                'and certain sanctioned parties. The 50 percent ownership standard in this IFR is '
                'designed to be consistent with longstanding Department of the Treasury practice, '
                'so as to limit the additional burden on the business community.'),
 '2025-19846': ('One Year Suspension of Expansion of End-User Controls for Affiliates of Certain '
                'Listed Entities',
                'In this final rule, the Bureau of Industry and Security (BIS) imposes a one-year '
                'suspension of the interim final rule, "Expansion of End-User Controls to Cover '
                'Affiliates of Certain Listed Entities,". The suspension is set to end November 9, '
                '2026, absent a future extension.'),
 '2025-19508': ('Additions to the Entity List',
                'In this rule, the Bureau of Industry and Security (BIS) amends the Export '
                'Administration Regulations (EAR) by adding 29 entries (26 entities and 3 '
                "addresses) to the Entity List under the destinations of People's Republic of "
                'China (China) (19), Turkey (9), and the United Arab Emirates (UAE) (1). These '
                'entities have been determined by the U.S. Government to be acting contrary to the '
                'national security or foreign policy interests of the United States.'),
 '2025-19858': ('Revisions to the Entity List',
                'The Bureau of Industry and Security (BIS) is removing one entity from the Entity '
                "List under the destination of China, People's Republic of (China). BIS is also "
                'removing six aliases associated with a different entity on the Entity List under '
                'the destination of China. BIS has determined, based on the review of additional '
                'information, that the entities do not pose a significant risk of being or '
                'becoming involved in activities that are contrary to the national security or '
                'foreign policy interests of the United States.'),
 '2026-00789': ('Revision to License Review Policy for Advanced Computing Commodities',
                'The Bureau of Industry and Security (BIS) is revising its license review policy '
                'for exports of certain semiconductors to China and Macau--changing it from a '
                'presumption of denial to a case-by-case review. The semiconductors covered by '
                'this rule are the Nvidia H200 and its equivalents, as well as less advanced '
                'chips--provided that (1) the semiconductors are commercially available in the '
                'United States at the time of publication of this rule and (2) the exporter '
                'certifies that: there is sufficient supply of this product in the United States; '
                'production of this product for exports to China will not divert global foundry '
                'capacity for similar or more advanced products for end users in the United '
                'States; the recipient has demonstrated sufficient security procedures; and the '
                'item undergoes independent, third-party testing in the United States to verify '
                'its performance specifications.'),
 '2026-06851': ('Extension of Authorized Integrated Circuit (IC) Designer Status and Application '
                'Deadline To Become an Approved IC Designer',
                'The Bureau of Industry and Security (BIS) is revising the Export Administration '
                'Regulations (EAR) by extending by about eight months the triggering date for '
                'authorized integrated circuit designer status and submission date for '
                'applications to become an approved integrated circuit (IC) designer. The new date '
                'is December 31, 2026.'),
 '2026-17230': ('Removal From the Entity List',
                'In this rule, the Bureau of Industry and Security (BIS) amends the Export '
                'Administration Regulations (EAR) by removing one entity from the Entity List '
                'under the destination of Turkey.'),
 '2026-17231': ('Revisions to the Entity List',
                'In this rule, the Bureau of Industry and Security (BIS) revises the Export '
                'Administration Regulations (EAR) by removing two addresses associated with Arrow '
                'Electronics (Hong Kong) Co., Ltd. from the Entity List under the destination of '
                "China, People's Republic of (China). This determination follows the removal from "
                'the Entity List of Arrow China Electronics Trading Co., Ltd., and the removal of '
                'six aliases for Arrow Electronics (Hong Kong) Co., Ltd. in November 2025.'),
 '2026-19537': ('Measures To Restrict Stockpiling of Polysilicon and Polysilicon Derivatives Under '
                'Proclamation 11052',
                'On August 6, 2026, the President issued Proclamation 11052, "Adjusting Imports of '
                'Polysilicon and Its Derivatives Into the United States" (Proclamation 11052), '
                'ordering the Secretary of Commerce (Secretary) to take action to restrict imports '
                'by a company if he determines the company is stockpiling polysilicon or '
                'polysilicon derivatives (Polysilicon Products) in advance of import adjustments '
                'that will be effective on December 4, 2026. The Bureau of Industry and Security '
                '(BIS), in this temporary final rule (TFR), announces the criteria and process it '
                'will use to monitor existing companies for evidence of stockpiling, limit a newly '
                "established importer's ability to stockpile, and subject companies that are "
                'stockpiling to an import prohibition if necessary. This TFR also establishes the '
                'process for such companies to obtain a waiver from any import prohibitions '
                'imposed pursuant to this rule, and imposes certain import limitations on new '
                'importers that register with U.S. Customs and Border Protection (CBP) on or after '
                'August 6, 2026.'),
 '2026-14132': ('Enhanced Favorable Treatment for the United Arab Emirates Under the Export '
                'Administration Regulations',
                'In this final rule, the Bureau of Industry and Security (BIS) amends the Export '
                'Administration Regulations (EAR) to provide enhanced favorable treatment for the '
                'United Arab Emirates (UAE). Specifically, BIS is removing the UAE from Country '
                'Groups D:3 and D:4 and adding the UAE to Country Group A:5. More license '
                'exceptions will now be available, including Strategic Trade Authorization (STA) '
                'for the UAE Government and approved commercial entities in the UAE. STA will '
                'authorize the export, reexport, or transfer (in-country) of military items; '
                'certain commercial satellites and spacecraft; and dual-use items useful in, inter '
                'alia, oil and gas production, desalination, and civil nuclear power generation. '
                'The UAE Government and approved commercial entities will also have license-free '
                'access to advanced computing items, consistent with the May 2025 U.S.-UAE '
                'Artificial Intelligence Cooperation framework, without compromising U.S. digital '
                'infrastructure buildout.'),
 '2025-16735': ("Revocation of Validated End-User Authorizations in the People's Republic of China",
                'In this final rule, the Bureau of Industry and Security (BIS) amends the Export '
                'Administration Regulations (EAR) to revise the existing Validated End-User (VEU) '
                "Authorizations list for the People's Republic of China (PRC) by removing Intel "
                'Semiconductor (Dalian) Ltd; Samsung China Semiconductor Co. Ltd; and SK hynix '
                'Semiconductor (China) Ltd.'),
 '2025-02655': ('Implementation of Additional Due Diligence Measures for Advanced Computing '
                'Integrated Circuits; Amendments and Clarifications; and Extension of Comment '
                'Period; Correction',
                'On January 16, 2025, BIS published in the Federal Register an interim final rule '
                '(IFR), "Implementation of Additional Due Diligence Measures for Advanced '
                'Computing Integrated Circuits; Amendments and Clarifications; and Extension of '
                'Comment Period" (January 16 IFR). This rule revises Export Control Classification '
                "Number (ECCN) 3A090 to correct this ECCN's license requirement added in the "
                'January 16 IFR.'),
 '2024-19633': ('Commerce Control List Additions and Revisions; Implementation of Controls on '
                'Advanced Technologies Consistent With Controls Implemented by International '
                'Partners',
                'The Bureau of Industry and Security (BIS) is implementing export controls on '
                'several semiconductor, quantum, and additive manufacturing items for national '
                'security and foreign policy reasons. This rule adds new Export Control '
                'Classification Numbers (ECCNs) to the Commerce Control List, revises existing '
                'ECCNs, adds a new license exception to authorize exports and reexports to and by '
                'countries that have implemented equivalent technical controls for these newly '
                'added items, and adds two new worldwide license requirements to the national '
                'security and regional stability controls in the Export Administration Regulations '
                '(EAR). These controls are the product of extensive discussions with international '
                'partners.'),
 '2022-21658': ('Implementation of Additional Export Controls: Certain Advanced Computing and '
                'Semiconductor Manufacturing Items; Supercomputer and Semiconductor End Use; '
                'Entity List Modification',
                'In this rule, the Bureau of Industry and Security (BIS) is amending the Export '
                'Administration Regulations (EAR) to implement necessary controls on advanced '
                'computing integrated circuits (ICs), computer commodities that contain such ICs, '
                'and certain semiconductor manufacturing items. In addition, BIS is expanding '
                'controls on transactions involving items for supercomputer and semiconductor '
                'manufacturing end uses, for example, this rule expands the scope of '
                'foreign-produced items subject to license requirements for twenty-eight existing '
                'entities on the Entity List that are located in China. BIS is also informing the '
                'public that specific activities of "U.S. persons" that `support\' the '
                '"development" or "production" of certain ICs in the PRC require a license. '
                'Lastly, to minimize short term impact on the semiconductor supply chain from this '
                'rule, BIS is establishing a Temporary General License to permit specific, limited '
                'manufacturing activities in China related to items destined for use outside China '
                'and is identifying a model certificate that may be used in compliance programs to '
                'assist, along with other measures, in conducting due diligence. `'),
 '2023-23049': ('Export Controls on Semiconductor Manufacturing Items',
                'On October 7, 2022, the Bureau of Industry and Security (BIS) released the '
                'interim final rule (IFR) "Implementation of Additional Export Controls: Certain '
                'Advanced Computing and Semiconductor Manufacturing Items; Supercomputer and '
                'Semiconductor End Use" (October 7 IFR), which amended the Export Administration '
                'Regulations (EAR) to implement controls on advanced computing integrated circuits '
                '(ICs), computer commodities that contain such ICs, and certain semiconductor '
                'manufacturing items. The October 7 IFR also made other EAR changes to ensure '
                'appropriate related controls, including on certain "U.S. person" activities. This '
                'IFR addresses comments received in response to only the part of the October 7 IFR '
                'that controls semiconductor manufacturing equipment (SME) and amends the EAR to '
                'implement SME controls more effectively and to address ongoing national security '
                'concerns.'),
 '2023-27588': ('Export Controls on Semiconductor Manufacturing Items; Implementation of '
                'Additional Export Controls: Certain Advanced Computing Items; Supercomputer and '
                'Semiconductor End Use; Updates and Corrections; Extension of Comment Period',
                'On October 25, 2023, the Bureau of Industry and Security (BIS) published in the '
                'Federal Register the interim final rules (IFR), "Export Controls on Semiconductor '
                'Manufacturing Items" (SME IFR) and "Implementation of Additional Export Controls: '
                'Certain Advanced Computing Items; Supercomputer and Semiconductor End Use; '
                'Updates and Corrections" (AC/S IFR). This notification extends the deadline for '
                'submission of written comments on both rules to January 17, 2024. BIS is making '
                'this extension to allow commenters to have additional time to review the interim '
                'final rules and to benefit from the significant amount of public outreach that '
                'BIS is conducting on the rules prior to preparing and submitting their comments '
                'on the IFRs.'),
 '2025-00636': ('Framework for Artificial Intelligence Diffusion',
                "With this interim final rule, the Commerce Department's Bureau of Industry and "
                "Security (BIS) revises the Export Administration Regulations' (EAR) controls on "
                'advanced computing integrated circuits (ICs) and adds a new control on artificial '
                'intelligence (AI) model weights for certain advanced closed-weight dual-use AI '
                'models. In conjunction with the expansion of these controls, which BIS has '
                'determined are necessary to protect U.S. national security and foreign policy '
                'interests, BIS is adding new license exceptions and updating the Data Center '
                'Validated End User authorization to facilitate the export, reexport, and transfer '
                '(in-country) of advanced computing (ICs) to end users in destinations that do not '
                'raise national security or foreign policy concerns. Together, these changes will '
                'cultivate secure ecosystems for the responsible diffusion and use of AI and '
                'advanced computing ICs.'),
 '2025-00637': ('Public Briefing on Framework for Artificial Intelligence Diffusion',
                'On January 10, 2025, the Office of the Federal Register posted for public '
                'inspection a Bureau of Industry and Security (BIS) interim final rule: "Framework '
                'for Artificial Intelligence Diffusion" (RIN 0694-AJ90). This document announces '
                'that, on January 15, 2025, BIS will host a virtual public briefing on this rule. '
                'This document also provides details on the procedures for participating in the '
                'virtual public briefing.'),
 '2024-22587': ('Expansion of Validated End User Authorization: Data Center Validated End User '
                'Authorization',
                'In this rule, the Department of Commerce, Bureau of Industry and Security (BIS), '
                'amends the Export Administration Regulations (EAR) to expand the Validated End '
                'User Authorization (VEU) program to include VEU Authorization for data centers '
                'located in specified destinations ("Data Center VEU" or "Data Center VEU '
                'Authorization"). This expansion of the VEU program to include Data Center VEU is '
                'intended to facilitate quick and reliable export or reexport of items on the '
                'Commerce Control List necessary for a data center, including advanced computing '
                'items, to preapproved trusted end users. Data Center VEU adopts much of the '
                'framework of the existing VEU program, with additional requirements. This '
                'expansion of eligibility is intended to update the VEU program to recognize the '
                'advancement and benefits of artificial intelligence. As under the original VEU '
                'Authorization Program, the U.S. government will rigorously review Data Center VEU '
                "candidates' applications subject to detailed and verifiable criteria."),
 '2024-28267': ('Additions and Modifications to the Entity List; Removals From the Validated '
                'End-User (VEU) Program',
                'In this final rule, the Bureau of Industry and Security (BIS) amends the Export '
                'Administration Regulations (EAR) by adding 140 entities to the Entity List. These '
                "entries are listed on the Entity List under the destinations of China, People's "
                'Republic of (China), Japan, South Korea, and Singapore and have been determined '
                'by the U.S. Government to be acting contrary to the national security and foreign '
                'policy interests of the United States. This final rule also modifies 14 existing '
                'entries on the Entity List, consisting of revisions to 14 entries under China. '
                "This final rule publishes concurrently with BIS's interim final rule, "
                '"Foreign-Produced Direct Product Rule Additions, and Refinements to Controls for '
                'Advanced Computing and Semiconductor Manufacturing Items" (0694-AJ74), which '
                'makes additional changes to the EAR controls on advanced computing items and '
                'semiconductor manufacturing items. This final rule is part of this larger effort '
                'to ensure that appropriate EAR controls are in place on these items, including in '
                'connection with transactions destined to or otherwise involving the entities '
                'being added to the Entity List, as well as for existing entries on the Entity '
                'List that are being modified. All of these entities (those newly added and those '
                'being modified) are involved with the development and production of '
                '"advanced-node integrated circuits" ("advanced-node ICs") and/or semiconductor '
                "manufacturing items, and/or have supported the Chinese government's "
                'Military-Civil Fusion (MCF) Development Strategy. Additionally, this final rule '
                'designates nine of these entities being added and seven of the entries being '
                'modified as entities for which entity-specific restrictions involving '
                'foreign-produced items apply. This final rule also amends the EAR by removing '
                'three entities from the Validated End- User (VEU) Program.'),
 '2024-28423': ('Public Briefing on Changes to Advanced Computing and Semiconductor Manufacturing '
                'Items',
                'On December 2, 2024, the Office of the Federal Register posted for public '
                'inspection two related Bureau of Industry and Security (BIS) rules: an interim '
                'final rule, "Foreign-Produced Direct Product Rule Additions, and Refinements to '
                'Controls for Advanced Computing and Semiconductor Manufacturing Items," (RIN '
                '0694-AJ74) and a final rule, "Additions and Modifications to the Entity List; '
                'Removals from the Validated End-User (VEU) Program" (RIN 0694-AJ77). This '
                'document announces that, on December 5, 2024, BIS will host a virtual public '
                'briefing on these rules. This document also provides details on the procedures '
                'for participating in the virtual public briefing.'),
 '2026-02262': ('Conforming Change to the Export Administration Regulations for Cambodia',
                'In this final rule, the Bureau of Industry and Security (BIS) makes a conforming '
                'change to the Export Administration Regulations (EAR) to reflect that Cambodia is '
                'no longer a Country Group D:5 country. On November 7, 2025, the Department of '
                'State published a final rule, "International Traffic in Arms Regulations: Changes '
                'to Section 126.1," that removed Cambodia as an arms embargoed destination under '
                'the International Traffic in Arms Regulations (ITAR), pursuant to a determination '
                'made by the Secretary of State.'),
 '2025-16724': ('Relaxing Export Controls for Syria',
                'In this final rule, the Bureau of Industry and Security (BIS) makes changes to '
                'the Syria export control measures under the Export Administration Regulations '
                '(EAR), consistent with Executive Order (E.O.) 14312, Providing for the Revocation '
                'of Syria Sanctions, which directed the removal of sanctions on Syria. This final '
                "rule relaxes the EAR's existing restrictions on exports and reexports to Syria of "
                'items subject to the EAR by making the following changes: revising certain '
                'restrictive license application review policies that had applied to most items '
                'subject to the EAR to be more favorable; expanding existing license exceptions to '
                'apply to Syria; and adding new license exceptions for Syria, including for EAR99 '
                'items.'),
 '2024-08813': ('Revision of Firearms License Requirements',
                'In this interim final rule (IFR), the Bureau of Industry and Security (BIS) is '
                'amending the Export Administration Regulations (EAR) to enhance the control '
                'structure for firearms and related items. These changes will better protect U.S. '
                'national security and foreign policy interests, which include countering the '
                'diversion and misuse of firearms and related items and advancing human rights. '
                'This rule identifies semi-automatic firearms under new Export Control '
                'Classification Numbers (ECCNs); adds additional license requirements for Crime '
                'Control and Detection (CC) items, thereby resulting in additional restrictions on '
                'the availability of license exceptions for most destinations; amends license '
                'review policies so that they are more explicit as to the nature of review that '
                'will accompany different types of transactions and license exception availability '
                '(including adding a new list of high-risk destinations); updates and expands '
                'requirements for support documentation submitted with license applications; and '
                'better accounts for the import documentation requirements of other countries '
                '(such as an import certificate or other permit prior to importation) when '
                'firearms and related items are authorized under a BIS license exception. BIS is '
                'publishing this rule as an IFR to solicit comments from the public on additional '
                'changes to export controls on firearms and related items that would better '
                'protect U.S. national security and foreign policy interests.'),
 '2026-01059': ('Streamlining Export Controls for Drone Exports',
                'The Bureau of Industry and Security (BIS) is easing export controls on certain '
                'civil Unmanned Aerial Vehicles (UAVs) and related technologies, which currently '
                'need a license to be exported to most countries. In particular, this interim '
                'final rule (IFR): a) allows less sensitive UAVs--namely, commercial UAVs with a '
                'maximum endurance of less than one hour, for which there is broad foreign '
                'availability--to be exported to most Wassenaar Arrangement Participating States '
                '(Country Group A:1) without a license; and b) allows more capable non-military '
                'UAVs--namely, certain long-range cargo delivery and agricultural spraying '
                'drones--to be exported to certain U.S. partners and allies (Country Group A:5) '
                'under License Exception Strategic Trade Authorization (STA). Exports pursuant to '
                'License Exception STA are subject to notification and reporting requirements to '
                'ensure the security of the exports. BIS is making these changes pursuant to '
                'Executive Order (E.O.) 14307, "Unleashing American Drone Dominance."'),
 '2024-05267': ('Clarification of Controls on Radiation Hardened Integrated Circuits and Expansion '
                'of License Exception GOV',
                'The Bureau of Industry and Security (BIS) is amending the Export Administration '
                'Regulations (EAR) to clarify controls on radiation hardened integrated circuits, '
                'including controls on computer and telecommunications equipment incorporating '
                'such radiation hardened integrated circuits. This rule also addresses certain '
                'scenarios that apply to certain integrated circuits acquired, tested, or '
                'otherwise used by or for the United States Government and affirms the '
                'availability of License Exception GOV for such items when pursuant to an official '
                'written request or directive from the Department of Defense or the Department of '
                'Energy. Lastly, this rule expands the availability of License Exception GOV for '
                'microelectronics items being exported, reexported, or transferred (in-country) in '
                'furtherance of a contract between the exporter, reexporter, or transferor and a '
                'department or agency of the U.S. Government when the contract provides for the '
                'export, reexport, transfer (in-country) of the item by the exporter, reexporter, '
                'or transferor in order to remove export control obstacles for official business '
                'of the U.S. Government, including the Department of Energy and the Department of '
                'Defense.'),
 '2022-19415': ('Authorization of Certain “Items” to Entities on the Entity List in the Context of '
                'Specific Standards Activities',
                'In this interim final rule, the Bureau of Industry and Security (BIS) amends the '
                'Export Administration Regulations (EAR) to authorize the release of specified '
                'items subject to the EAR without a license when that release occurs in the '
                'context of a "standards- related activity," as defined in this rule. BIS is '
                'revising the terms used in the EAR to describe the actions permissible under the '
                'authorization rather than defining the organizations to which it applies. The '
                'scope of the authorization is revised to include certain "technology" as well as '
                '"software" and applies to all entities listed on BIS\'s Entity List. The '
                'uncertainty of not knowing whether other entities listed on the Entity List are '
                'participants in standards organizations and whether a BIS license is required to '
                'release low- level technology for legitimate standards activities has caused U.S. '
                'companies to limit their participation in standards-related activities in areas '
                'that are critical to U.S. national security. This authorization only overcomes '
                "licensing requirements imposed as a result of an entity's inclusion on the Entity "
                'List; other EAR licensing requirements, including additional end-use or end-user '
                'based licensing requirements may continue to apply. This final rule does not '
                'change the assessment of whether "technology" or "software" is subject to the '
                'EAR. BIS is making these revisions to ensure that export controls and associated '
                'compliance concerns as they relate to the Entity List do not impede the '
                'leadership and participation of U.S. companies in national and international '
                'standards-related activities'),
 '2022-11614': ('Control Policy: End-User and End-Use Based', None),
 '2025-22137': ('The Commerce Control List', None),
 '2022-04925': ('Further Imposition of Sanctions Against Russia With the Addition of Certain '
                'Entities to the Entity List',
                "In response to the Russian Federation's (Russia's) further invasion of Ukraine on "
                'February 24, 2022, the Department of Commerce is amending the Export '
                'Administration Regulations (EAR) by adding 91 new entities to the Entity List '
                'under the destinations of Belize, Estonia, Kazakhstan, Latvia, Malta, Russia, '
                'Singapore, Slovakia, Spain, and United Kingdom with this final rule. These 91 '
                'entities have been determined by the U.S. Government to be acting contrary to the '
                'foreign policy or national security interests of the United States.'),
 '2022-17125': ('Implementation of Certain 2021 Wassenaar Arrangement Decisions on Four Section '
                '1758 Technologies',
                'The Bureau of Industry and Security (BIS) maintains, as part of its Export '
                'Administration Regulations (EAR), the Commerce Control List (CCL), which '
                'identifies certain items subject to Department of Commerce (Commerce) '
                'jurisdiction. Commerce is revising the CCL, as well as corresponding parts of the '
                'EAR, to implement controls on four technologies. These changes reflect certain '
                'controls decided by governments participating in the Wassenaar Arrangement on '
                'Export Controls for Conventional Arms and Dual-Use Goods and Technologies (WA) at '
                'the December 2021 WA Plenary meeting. These four technologies meet the criteria '
                'of Section 1758 of the Export Control Reform Act (ECRA) pertaining to emerging '
                'and foundational technologies. Accordingly, BIS is accelerating their publication '
                'in this interim final rule and will publish the remaining WA-agreed controls in a '
                'later rule. These technologies are two substrates of ultra-wide bandgap '
                'semiconductors (Gallium Oxide (Ga<INF>2</INF>O<INF>3</INF>) and diamond), '
                'Electronic Computer Aided Design (ECAD) software specially designed for the '
                'development of integrated circuits with any Gate-All-Around Field-Effect '
                'Transistor (GAAFET) structure, and pressure gain combustion (PGC) technology for '
                'the production and development of gas turbine engine components or systems.'),
 '2023-22873': ("Existing Validated End-User Authorizations in the People's Republic of China: "
                'Samsung China Semiconductor Co. Ltd. and SK Hynix Semiconductor (China) Ltd.',
                'In this rule, the Bureau of Industry and Security (BIS) amends the Export '
                'Administration Regulations (EAR) to revise the existing Validated End-User (VEU) '
                "list for the People's Republic of China (PRC) by updating the list of eligible "
                'items in the EAR for Samsung China Semiconductor Co. Ltd. and SK hynix '
                'Semiconductor (China) Ltd. In addition, this rule makes corresponding changes '
                'consistent with the scope of the amended authorizations for these VEUs.'),
 '2024-28270': ('Foreign-Produced Direct Product Rule Additions, and Refinements to Controls for '
                'Advanced Computing and Semiconductor Manufacturing Items',
                'In this interim final rule (IFR), the Bureau of Industry and Security (BIS) makes '
                'changes to the Export Administration Regulations (EAR) controls for certain '
                'advanced computing items, supercomputers, and semiconductor manufacturing '
                'equipment, which includes adding new controls for certain semiconductor '
                'manufacturing equipment and related items, creating new Foreign Direct Product '
                '(FDP) rules for certain commodities to impair the capability to produce '
                '"advanced-node integrated circuits" ("advanced-node ICs") by certain destinations '
                'or entities of concern, adding new controls for certain high bandwidth memory '
                'important for advanced computing, and clarifying controls on certain software '
                'keys that allow for the use of items such as software tools. This IFR publishes '
                'concurrently with another BIS final rule entitled, "Additions and Modifications '
                'to the Entity List; and Removals from the Validated End-User (VEU) Program" '
                '(Entity List rule) that adds to and modifies the Entity List to ensure '
                'appropriate EAR controls are in place for certain critical technologies and to '
                'minimize the risk of diversion to entities of concern.')}


def title_of(doc: str) -> str:
    return REAL_RULES[doc][0]


def abstract_of(doc: str) -> str | None:
    return REAL_RULES[doc][1]


# (document_number, expected kind, expected relevant). Comments mark the cases
# where the kind is a judgment call rather than an unambiguous reading.
EXPECTED = [
    # 50%-affiliates Entity List rule and its one-year suspension
    ("2025-19001", "affiliates_rule", True),
    ("2025-19846", "affiliates_rule", True),
    # named-entity Entity List actions: kept as ExportControl nodes, never linked
    ("2025-19508", "entity_list_additions", False),   # 29 entries added
    ("2025-19858", "entity_list_additions", False),   # a removal + aliases (still "modifies named entities")
    ("2026-17230", "entity_list_additions", False),   # removal of one Turkish entity
    ("2026-17231", "entity_list_additions", False),   # removal of Arrow addresses
    ("2022-04925", "entity_list_additions", False),   # Russia sanctions + Entity List additions: the named-entity part
    # chips / computing
    ("2025-02655", "advanced_computing", True),
    ("2022-21658", "advanced_computing", True),
    ("2024-28270", "advanced_computing", True),
    ("2024-28423", "advanced_computing", True),       # public briefing, title names advanced computing
    # JUDGMENT: both advanced computing and semiconductor manufacturing appear in the title;
    # advanced computing wins because the chips themselves are what company disclosures cite.
    ("2023-27588", "advanced_computing", True),
    ("2023-23049", "semiconductor_equipment", True),
    # JUDGMENT: title names no chips at all; the abstract says "semiconductor, quantum, and
    # additive manufacturing items" (GAAFET / SME controls), so the abstract decides.
    ("2024-19633", "semiconductor_equipment", True),
    # JUDGMENT: ultra-wide-bandgap substrates + GAAFET ECAD software: semiconductor technology
    # controls, closest to semiconductor_equipment.
    ("2022-17125", "semiconductor_equipment", True),
    # licensing policy
    # JUDGMENT: a review-policy change for advanced computing chips; "License Review Policy" in the
    # title makes it licensing_policy even though "Advanced Computing" also appears.
    ("2026-00789", "licensing_policy", True),
    ("2026-06851", "licensing_policy", True),         # authorized IC designer status / deadline
    ("2026-14132", "licensing_policy", True),         # UAE moved to Country Group A:5 (advanced computing / AI context)
    ("2024-22587", "licensing_policy", True),         # Data Center VEU
    ("2025-16735", "licensing_policy", True),         # VEU revocation naming Intel / Samsung / SK hynix fabs in China
    ("2023-22873", "licensing_policy", True),
    # JUDGMENT: 140 Entity List additions AND removal of Samsung/SK hynix VEU: when a rule mixes a
    # non-relevant kind with a relevant one, relevant wins (recall for company linking).
    ("2024-28267", "licensing_policy", True),
    # AI
    ("2025-00636", "ai_model_controls", True),
    ("2025-00637", "ai_model_controls", True),
    # everything else
    ("2026-19537", "other", False),                   # polysilicon import stockpiling: not a chip export control
    ("2026-02262", "other", False),                   # Cambodia leaves Country Group D:5
    ("2025-16724", "other", False),                   # Syria: license exceptions, no chip context
    ("2024-08813", "other", False),                   # firearms license review policies
    ("2026-01059", "other", False),                   # drones: Country Group A:5 / STA
    # JUDGMENT: radiation-hardened ICs are a space / military-electronics carve-out, not the
    # advanced-computing or SME regime the company disclosures discuss.
    ("2024-05267", "other", False),
    # Entity List is in the title but it authorises standards activities; no named entities change.
    ("2022-19415", "other", False),
    ("2022-11614", "other", False),                   # no abstract at all
    ("2025-22137", "other", False),                   # no abstract at all
]


class TestClassifyRuleOnRealText:
    @pytest.mark.parametrize("doc,kind,relevant", EXPECTED, ids=[e[0] for e in EXPECTED])
    def test_kind_and_relevance(self, doc, kind, relevant):
        got = classify_rule(title_of(doc), abstract_of(doc))
        assert got["kind"] == kind
        assert got["relevant"] is relevant

    def test_only_five_kinds_are_relevant(self):
        assert RELEVANT_KINDS == {
            "advanced_computing", "semiconductor_equipment", "affiliates_rule",
            "licensing_policy", "ai_model_controls",
        }
        assert set(RULE_KINDS) == RELEVANT_KINDS | {"entity_list_additions", "other"}

    def test_result_shape(self):
        got = classify_rule(title_of("2025-19001"), abstract_of("2025-19001"))
        assert set(got) == {"kind", "topics", "relevant"}
        assert isinstance(got["topics"], list) and all(isinstance(t, str) for t in got["topics"])

    def test_topics_are_sorted_and_unique(self):
        got = classify_rule(title_of("2026-14132"), abstract_of("2026-14132"))
        assert got["topics"] == sorted(set(got["topics"]))

    def test_topics_reflect_the_text(self):
        aff = classify_rule(title_of("2025-19001"), abstract_of("2025-19001"))["topics"]
        assert {"affiliates rule", "entity list"} <= set(aff)
        h200 = classify_rule(title_of("2026-00789"), abstract_of("2026-00789"))["topics"]
        assert {"advanced computing", "license review"} <= set(h200)
        uae = classify_rule(title_of("2026-14132"), abstract_of("2026-14132"))["topics"]
        assert {"country group", "advanced computing", "artificial intelligence"} <= set(uae)
        sme = classify_rule(title_of("2023-23049"), abstract_of("2023-23049"))["topics"]
        assert "semiconductor manufacturing" in sme
        ai = classify_rule(title_of("2025-00636"), abstract_of("2025-00636"))["topics"]
        assert "artificial intelligence" in ai
        veu = classify_rule(title_of("2025-16735"), abstract_of("2025-16735"))["topics"]
        assert "validated end-user" in veu

    def test_unrelated_rules_have_no_topics(self):
        assert classify_rule(title_of("2024-08813"), abstract_of("2024-08813"))["topics"] == []

    def test_none_and_empty_abstract_do_not_crash(self):
        for abstract in (None, "", "   "):
            got = classify_rule("Additions to the Entity List", abstract)
            assert got["kind"] == "entity_list_additions" and got["relevant"] is False
        assert classify_rule("", None) == {"kind": "other", "topics": [], "relevant": False}

    def test_is_deterministic_and_pure(self):
        args = (title_of("2026-00789"), abstract_of("2026-00789"))
        assert classify_rule(*args) == classify_rule(*args)

    def test_case_insensitive(self):
        a = classify_rule("REVISION TO LICENSE REVIEW POLICY FOR ADVANCED COMPUTING COMMODITIES", None)
        assert a["kind"] == "licensing_policy"

    def test_entity_list_abstract_mentioning_chips_stays_an_entity_list_rule(self):
        # Entity List additions routinely cite advanced-computing reasons; the title decides.
        got = classify_rule(
            "Additions to the Entity List",
            "BIS adds entities that acquired advanced computing integrated circuits and semiconductor "
            "manufacturing items for diversion.",
        )
        assert got["kind"] == "entity_list_additions" and got["relevant"] is False


# ----------------------------------------------------------- fetch pipeline

def rule(doc: str, day: str, title: str = "Additions to the Entity List", abstract: str | None = "x") -> dict:
    return {
        "document_number": doc, "title": title, "publication_date": day,
        "html_url": f"https://www.federalregister.gov/d/{doc}", "pdf_url": f"https://govinfo/{doc}.pdf",
        "abstract": abstract, "effective_on": day, "citation": "1 FR 1", "type": "Rule",
    }


class FakeFR:
    """A canned, multi-page Federal Register: pages are linked by next_page_url."""

    def __init__(self, pages: list[list[dict]], count: int | None = None):
        self.pages = pages
        self.count = count if count is not None else sum(len(p) for p in pages)
        self.urls: list[str] = []

    def __call__(self, url: str) -> dict:
        self.urls.append(url)
        index = int(url.rsplit("/", 1)[1]) if "/next/" in url else 0
        body = {"count": self.count, "total_pages": len(self.pages), "results": self.pages[index]}
        if index + 1 < len(self.pages):
            body["next_page_url"] = f"https://fr.test/next/{index + 1}"
        return body


PAGES = [
    [rule("2026-00789", "2026-01-15", "Revision to License Review Policy for Advanced Computing Commodities"),
     rule("2026-00002", "2026-01-10")],
    [rule("2025-19001", "2025-09-30", "Expansion of End-User Controls To Cover Affiliates of Certain Listed Entities"),
     rule("2025-19508", "2025-10-09")],
    [rule("2022-04925", "2022-03-09")],
]


@pytest.fixture
def settings(tmp_path, monkeypatch) -> Settings:
    monkeypatch.setattr(FR.time, "sleep", lambda s: None)
    return Settings(data_dir=tmp_path / "data", sec_user_agent="Test test@example.com", _env_file=None)


class TestFetchAll:
    def test_follows_pagination_to_the_end(self):
        fr = FakeFR(PAGES)
        rules, pages = FR.fetch_all_bis_rules(fr)
        assert pages == 3 and len(rules) == 5
        assert fr.urls[1:] == ["https://fr.test/next/1", "https://fr.test/next/2"]

    def test_first_request_is_the_bis_rule_query_without_a_term(self):
        fr = FakeFR(PAGES)
        FR.fetch_all_bis_rules(fr)
        first = fr.urls[0]
        assert "industry-and-security-bureau" in first and "RULE" in first
        assert "2022-01-01" in first and "per_page=1000" in first
        assert "term" not in first          # the OR-term query silently behaved as AND (recall bug)
        for field in ("document_number", "title", "publication_date", "html_url", "pdf_url",
                      "abstract", "effective_on", "citation", "type"):
            assert f"fields%5B%5D={field}" in first

    def test_duplicates_across_pages_collapse_by_document_number(self):
        dup = [PAGES[0], [PAGES[0][0], rule("2025-19508", "2025-10-09")]]
        rules, _ = FR.fetch_all_bis_rules(FakeFR(dup, count=3))
        assert [r["document_number"] for r in rules] == ["2026-00789", "2026-00002", "2025-19508"]

    def test_count_mismatch_is_logged_not_fatal(self, caplog):
        with caplog.at_level("WARNING", logger="semigraph.ingestion.federal_register"):
            rules, _ = FR.fetch_all_bis_rules(FakeFR(PAGES, count=99))
        assert len(rules) == 5 and "99" in caplog.text

    def test_runaway_pagination_is_bounded(self):
        def endless(url: str) -> dict:
            return {"count": 1, "results": [rule("d", "2026-01-01")], "next_page_url": "https://fr.test/again"}

        with pytest.raises(RuntimeError, match="pages"):
            FR.fetch_all_bis_rules(endless)


class TestRetry:
    def test_transient_errors_are_retried_with_backoff(self, monkeypatch):
        sleeps: list[float] = []
        monkeypatch.setattr(FR.time, "sleep", sleeps.append)
        calls = {"n": 0}

        def flaky(url: str) -> dict:
            calls["n"] += 1
            if calls["n"] < 4:
                raise urllib.error.URLError("dns blip")
            return {"count": 0, "results": []}

        assert FR.with_retries(flaky)("u") == {"count": 0, "results": []}
        assert sleeps == [5, 15, 45] == FR.RETRY_WAITS_S

    def test_gives_up_after_four_tries_with_a_clear_error(self, monkeypatch):
        monkeypatch.setattr(FR.time, "sleep", lambda s: None)

        def down(url: str) -> dict:
            raise urllib.error.URLError("down")

        with pytest.raises(RuntimeError, match="unreachable after 4 tries"):
            FR.with_retries(down)("u")

    def test_client_errors_are_not_retried(self, monkeypatch):
        slept: list[float] = []
        monkeypatch.setattr(FR.time, "sleep", slept.append)

        def bad(url: str) -> dict:
            raise urllib.error.HTTPError(url, 400, "Bad Request", {}, None)  # type: ignore[arg-type]

        with pytest.raises(urllib.error.HTTPError):
            FR.with_retries(bad)("u")
        assert slept == []


class TestDownloadBisRules:
    def test_writes_cache_with_metadata_and_classification(self, settings):
        fr = FakeFR(PAGES)
        rules = FR.download_bis_rules(settings, fetch=fr)

        cache = json.loads(FR.rules_cache_path(settings).read_text(encoding="utf-8"))
        assert cache["count"] == 5 == len(cache["results"]) == len(rules)
        assert datetime.fromisoformat(cache["retrieved_at"]).tzinfo is not None
        q = cache["query"]
        assert q["agency"] == "industry-and-security-bureau" and q["type"] == "RULE"
        assert q["publication_date_gte"] == "2022-01-01" and q["pages"] == 3
        first = cache["results"][0]
        assert (first["kind"], first["relevant"]) == ("licensing_policy", True)
        assert "advanced computing" in first["topics"]
        assert all({"kind", "topics", "relevant"} <= set(r) for r in cache["results"])
        # loaders read these v1 fields unchanged
        assert {"document_number", "title", "publication_date", "html_url", "abstract"} <= set(first)

    def test_cache_is_reused_without_refresh(self, settings):
        FR.download_bis_rules(settings, fetch=FakeFR(PAGES))
        second = FakeFR(PAGES)
        rules = FR.download_bis_rules(settings, fetch=second)
        assert second.urls == [] and len(rules) == 5

    def test_refresh_refetches(self, settings):
        FR.download_bis_rules(settings, fetch=FakeFR(PAGES[:1]))
        again = FakeFR(PAGES)
        rules = FR.download_bis_rules(settings, refresh=True, fetch=again)
        assert again.urls and len(rules) == 5

    def test_legacy_v1_cache_is_refetched(self, settings):
        path = FR.rules_cache_path(settings)
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps({"results": [rule("OLD", "2025-09-16")], "description": "v1"}), encoding="utf-8")
        fr = FakeFR(PAGES)
        rules = FR.download_bis_rules(settings, fetch=fr)
        assert fr.urls and "OLD" not in {r["document_number"] for r in rules}
        assert "retrieved_at" in json.loads(path.read_text(encoding="utf-8"))

    def test_as_of_filters_the_returned_list_only(self, settings):
        rules = FR.download_bis_rules(settings, as_of="2025-10-09", fetch=FakeFR(PAGES))
        assert {r["document_number"] for r in rules} == {"2025-19001", "2025-19508", "2022-04925"}
        stored = json.loads(FR.rules_cache_path(settings).read_text(encoding="utf-8"))
        assert stored["count"] == 5          # the cache keeps everything

    def test_as_of_accepts_date_objects_and_is_inclusive(self, settings):
        rules = FR.download_bis_rules(settings, as_of=date(2026, 1, 10), fetch=FakeFR(PAGES))
        assert "2026-00002" in {r["document_number"] for r in rules}
        assert "2026-00789" not in {r["document_number"] for r in rules}

    def test_cached_read_still_applies_as_of(self, settings):
        FR.download_bis_rules(settings, fetch=FakeFR(PAGES))
        rules = FR.download_bis_rules(settings, as_of="2022-12-31", fetch=FakeFR([]))
        assert [r["document_number"] for r in rules] == ["2022-04925"]

    def test_default_fetcher_sends_the_declared_user_agent(self, settings, monkeypatch):
        seen: list[tuple[str, str]] = []

        def fake_get(url: str, user_agent: str) -> dict:
            seen.append((url, user_agent))
            return {"count": 0, "results": []}

        monkeypatch.setattr(FR, "_http_get_json", fake_get)
        FR.make_fetcher(settings)("https://fr.test/x")
        assert seen == [("https://fr.test/x", "Test test@example.com")]

    def test_default_fetcher_falls_back_to_a_generic_agent(self, tmp_path, monkeypatch):
        seen: list[str] = []
        monkeypatch.setattr(FR, "_http_get_json", lambda url, ua: seen.append(ua) or {})
        FR.make_fetcher(Settings(data_dir=tmp_path, sec_user_agent="", _env_file=None))("u")
        assert seen == ["semigraph"]


class TestHttpAndCorruptCache:
    def test_http_get_json_sends_the_user_agent_and_parses(self, monkeypatch):
        seen: dict = {}

        class Resp:
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def read(self) -> bytes:
                return b'{"count": 7, "results": []}'

        def fake_urlopen(req, timeout):
            seen.update(ua=req.get_header("User-agent"), url=req.full_url, timeout=timeout)
            return Resp()

        monkeypatch.setattr(FR.urllib.request, "urlopen", fake_urlopen)
        assert FR._http_get_json("https://fr.test/x", "Me me@example.com") == {"count": 7, "results": []}
        assert seen == {"ua": "Me me@example.com", "url": "https://fr.test/x", "timeout": FR.HTTP_TIMEOUT_S}

    def test_a_corrupt_cache_is_refetched(self, settings):
        path = FR.rules_cache_path(settings)
        path.parent.mkdir(parents=True)
        path.write_text("{not json", encoding="utf-8")
        fr = FakeFR(PAGES)
        assert len(FR.download_bis_rules(settings, fetch=fr)) == 5 and fr.urls

    def test_read_stored_rules_survives_a_corrupt_file(self, settings):
        path = FR.rules_cache_path(settings)
        path.parent.mkdir(parents=True)
        path.write_text("{not json", encoding="utf-8")
        assert FR.read_stored_rules(settings) == []

    def test_read_stored_rules_returns_legacy_results(self, settings):
        path = FR.rules_cache_path(settings)
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps({"results": [rule("OLD", "2025-09-16")]}), encoding="utf-8")
        assert [r["document_number"] for r in FR.read_stored_rules(settings)] == ["OLD"]

    def test_count_query_url_carries_the_upper_bound_only_when_given(self):
        assert "lte" not in FR.count_query_url()
        assert "publication_date%5D%5Blte%5D=2026-09-25" in FR.count_query_url(date(2026, 9, 25))


class TestAbstractFallbacks:
    """Titles with no signal are decided by the abstract."""

    def test_advanced_computing_in_the_abstract(self):
        got = classify_rule("Commerce Control List Additions and Revisions",
                            "BIS revises ECCN 3A090 and related advanced computing items.")
        assert got["kind"] == "advanced_computing" and got["relevant"] is True

    def test_ai_in_the_abstract(self):
        got = classify_rule("Revisions to the Export Administration Regulations",
                            "This rule controls the export of model weights of frontier systems.")
        assert got["kind"] == "ai_model_controls"

    def test_licensing_language_without_chip_context_stays_other(self):
        got = classify_rule("Revision of License Exception Availability", "Expands license exception STA for drones.")
        assert got["kind"] == "other" and got["relevant"] is False


class TestLegacyExports:
    def test_topic_keywords_still_exported(self):
        assert set(FR.TOPIC_KEYWORDS) == {
            "entity list", "advanced computing", "semiconductor manufacturing", "artificial intelligence",
        }

    def test_package_reexports(self):
        from semigraph.ingestion import TOPIC_KEYWORDS, download_bis_rules

        assert TOPIC_KEYWORDS is FR.TOPIC_KEYWORDS and download_bis_rules is FR.download_bis_rules

    def test_query_url_carries_no_term_condition(self):
        assert "conditions%5Bterm%5D" not in FR.fr_query_url()
